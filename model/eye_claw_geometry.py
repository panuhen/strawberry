"""V2 procedural inflated pincers and real eyelid geometry; used by build_strawberry.py."""
def puffy_pincer(points,y,depth,side):
    # Smooth the outline, then form curved front/back surfaces like an inflated volume.
    outline=[Vector((x,z)) for x,z in points]
    for repeat in range(3):
        result=[]
        for i,a in enumerate(outline):
            b=outline[(i+1)%len(outline)]
            result.extend((a*.75+b*.25,a*.25+b*.75))
        outline=result
    center=sum(outline,Vector((0,0)))/len(outline)
    half=depth*.82; segments=len(outline); rings=16
    vs=[(side*center.x,y-half,center.y)]; fs=[]
    for j in range(1,rings):
        angle=-math.pi/2+math.pi*j/rings
        radius=math.cos(angle)
        for p in outline:
            q=center+(p-center)*radius
            vs.append((side*q.x,y+half*math.sin(angle),q.y))
    pole=len(vs);vs.append((side*center.x,y+half,center.y))
    fs.extend((0,1+(i+1)%segments,1+i) for i in range(segments))
    for j in range(rings-2):
        for i in range(segments):
            a=1+j*segments+i;b=1+j*segments+(i+1)%segments
            fs.append((a,b,b+segments,a+segments))
    fs.extend((pole,1+(rings-2)*segments+i,1+(rings-2)*segments+(i+1)%segments) for i in range(segments))
    if side<0:fs=[tuple(reversed(f)) for f in fs]
    # XZ contour winding is opposite the outward Y-facing surface winding.
    return vs,[tuple(reversed(face)) for face in fs]

polygon_prism=puffy_pincer

# Both caps meet just below the eye centre. The lower rim reads as a lip.
LID_RADIUS=Vector((.054,.057,.058))

def lid_point(center,theta,phi,sign):
    return center+Vector((LID_RADIUS.x*math.sin(theta)*math.cos(phi),
        LID_RADIUS.y*math.sin(theta)*math.sin(phi),sign*LID_RADIUS.z*math.cos(theta)))

def add_lids():
    for side in ['L','R']:
        obj=bpy.data.objects['mesh_eye_'+side]
        old=obj.data;center=Vector(obj['eye_center'])
        verts=[tuple(v.co) for v in old.vertices]
        faces=[tuple(p.vertices) for p in old.polygons]
        materials=[p.material_index for p in old.polygons]
        segments=48;rings=12
        obj['lid_rings']=rings;obj['lid_segments']=segments
        for label,sign,angle in [('upper',1,.95),('lower',-1,.55)]:
            start=len(verts);obj[label+'_lid_start']=start
            verts.append(tuple(lid_point(center,0,0,sign)))
            for j in range(1,rings+1):
                for i in range(segments):
                    verts.append(tuple(lid_point(center,angle*j/rings,2*math.pi*i/segments,sign)))
            fs=[(start,start+1+i,start+1+(i+1)%segments) for i in range(segments)]
            for j in range(rings-1):
                for i in range(segments):
                    a=start+1+j*segments+i;b=start+1+j*segments+(i+1)%segments
                    fs.append((a,a+segments,b+segments,b))
            if sign<0:fs=[tuple(reversed(face)) for face in fs]
            faces.extend(fs);materials.extend([2]*len(fs))
        # A raised, rounded front edge follows the lower lid, including at rest.
        start=len(verts);obj['lower_rim_start']=start
        for i in range(33):
            phi=.12+(math.pi-.24)*i/32
            point=lid_point(center,.55,phi,-1)
            for j in range(8):
                a=2*math.pi*j/8
                verts.append(tuple(point+Vector((0,.0018*math.cos(a),.0018*math.sin(a)))))
        for i in range(32):
            for j in range(8):
                a=start+i*8+j;b=start+i*8+(j+1)%8
                faces.append((a,a+8,b+8,b));materials.append(1)
        data=bpy.data.meshes.new('eye_lid_temp');data.from_pydata(verts,[],faces);data.update()
        for name in ['mat_eye','mat_ink','mat_shell']:data.materials.append(bpy.data.materials[name])
        for face,material in zip(data.polygons,materials):face.material_index=material
        obj.data=data;bpy.data.meshes.remove(old);data.name=obj.name

def make_eye_keys(obj):
    obj.shape_key_add(name='Basis')
    center=Vector(obj['eye_center']);segments=obj['lid_segments'];rings=obj['lid_rings']
    for name,amount in [('blink',1.0),('squint',.60)]:
        key=obj.shape_key_add(name=name,from_mix=False)
        for label,sign,angle in [('upper',1,.95),('lower',-1,.55)]:
            start=obj[label+'_lid_start']
            closed=math.acos(sign*(-.008)/LID_RADIUS.z)
            maximum=angle+(closed-angle)*amount
            for j in range(1,rings+1):
                for i in range(segments):
                    key.data[start+1+(j-1)*segments+i].co=lid_point(center,maximum*j/rings,2*math.pi*i/segments,sign)
        theta=.55+(math.acos(.008/LID_RADIUS.z)-.55)*amount
        for i in range(33):
            phi=.12+(math.pi-.24)*i/32
            point=lid_point(center,theta,phi,-1)
            for j in range(8):
                a=2*math.pi*j/8
                key.data[obj['lower_rim_start']+i*8+j].co=point+Vector((0,.0018*math.cos(a),.0018*math.sin(a)))
    make_eye_wide_key(obj)
    make_happy_key(obj)

def make_eye_wide_key(obj):
    # Retract both lids for surprise without scaling or flattening the eyeball.
    if 'eye_wide' in obj.data.shape_keys.key_blocks:
        obj.shape_key_remove(obj.data.shape_keys.key_blocks['eye_wide'])
    key=obj.shape_key_add(name='eye_wide',from_mix=False)
    center=Vector(obj['eye_center']);segments=obj['lid_segments'];rings=obj['lid_rings']
    for label,sign,angle in [('upper',1,.38),('lower',-1,.25)]:
        start=obj[label+'_lid_start']
        for j in range(1,rings+1):
            for i in range(segments):
                key.data[start+1+(j-1)*segments+i].co=lid_point(center,angle*j/rings,2*math.pi*i/segments,sign)
    for i in range(33):
        phi=.12+(math.pi-.24)*i/32
        point=lid_point(center,.25,phi,-1)
        for j in range(8):
            a=2*math.pi*j/8
            key.data[obj['lower_rim_start']+i*8+j].co=point+Vector((0,.0018*math.cos(a),.0018*math.sin(a)))

def make_happy_key(obj):
    if "happy" in obj.data.shape_keys.key_blocks:
        obj.shape_key_remove(obj.data.shape_keys.key_blocks["happy"])
    key=obj.shape_key_add(name="happy",from_mix=False)
    center=Vector(obj["eye_center"])
    basis=obj.data.shape_keys.key_blocks["Basis"]
    # Both lids and the lower ink rim retract inside the intact eyeball.
    for index in range(obj["upper_lid_start"],len(key.data)):
        key.data[index].co=center+(basis.data[index].co-center)*.70
