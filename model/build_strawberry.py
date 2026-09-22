"""Reproducible Strawberry blockout. Run in Blender: exec(compile(open(...).read(),..., 'exec')); build(phase).
import os
build(n) regenerates phases 1..n; run_phase(n) reruns a phase on its prerequisites.
Front is +Y; all mesh coordinates are baked into mesh data before rigging.
"""
import bpy, math, json, os
from pathlib import Path
from mathutils import Vector, Matrix

OUT = Path(os.path.expanduser('~/strawberry/v2'))
OUT.mkdir(exist_ok=True)
COLORS = {'mat_shell':'cf2b28','mat_shell_dark':'7d1516','mat_claw':'e2402f','mat_cream':'f6e3cf','mat_eye':'fbf3e8','mat_ink':'201318'}
CLIPS = {'idle_loop':90,'listen_loop':60,'think_loop':60,'talk_base':40,'alert_snap':32,'notify_perk':32,'dance_loop':121,'sleep_enter':76,'sleep_loop':241,'wake_up':46}

def enum(obj, prop, value):
    values=[i.identifier for i in obj.bl_rna.properties[prop].enum_items]
    assert value in values, (prop,value,values)
    setattr(obj,prop,value)

def active(obj):
    for o in bpy.context.selected_objects: o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active=obj

def add_basic_uvs(data):
    """Continuous spherical coordinates avoid needless splits on smooth untextured surfaces."""
    uv=data.uv_layers.get('UVMap') or data.uv_layers.new(name='UVMap')
    lower=Vector(tuple(min(v.co[i] for v in data.vertices) for i in range(3)))
    upper=Vector(tuple(max(v.co[i] for v in data.vertices) for i in range(3)))
    center=(lower+upper)*.5
    coords=[]
    for v in data.vertices:
        q=v.co-center; length=max(q.length,1e-8)
        coords.append((.5+math.atan2(q.y,q.x)/(2*math.pi), math.acos(max(-1,min(1,q.z/length)))/math.pi))
    for face in data.polygons:
        indices=list(face.loop_indices)
        if len(indices)>4:
            points=[data.vertices[data.loops[i].vertex_index].co for i in indices]
            tangent=(points[1]-points[0]).normalized();bitangent=face.normal.cross(tangent).normalized()
            for i,point in zip(indices,points):uv.data[i].uv=(point.dot(tangent),point.dot(bitangent))
        else:
            values=[coords[data.loops[i].vertex_index] for i in indices]
            wrap=max(p[0] for p in values)-min(p[0] for p in values)>.5
            for i,(u,v) in zip(indices,values):uv.data[i].uv=(u+1 if wrap and u<.5 else u,v)


def mesh(name, verts, faces, mat):
    data=bpy.data.meshes.new(name)
    data.from_pydata(verts,[],faces); data.update()
    add_basic_uvs(data)
    obj=bpy.data.objects.new(name,data)
    bpy.data.collections['Strawberry'].objects.link(obj)
    obj['material_region']=mat
    data.materials.append(bpy.data.materials[mat])
    return obj

def ellipsoid(center, radii, seg=12, rings=6, orient=None):
    seg=max(seg,32); rings=max(rings,12)
    c=Vector(center); rot=orient or Matrix.Identity(3)
    v=[tuple(c+rot@Vector((0,0,radii[2])))]
    for j in range(1,rings):
        a=math.pi*j/rings
        for i in range(seg):
            t=2*math.pi*i/seg
            v.append(tuple(c+rot@Vector((radii[0]*math.sin(a)*math.cos(t),radii[1]*math.sin(a)*math.sin(t),radii[2]*math.cos(a)))))
    bottom=len(v); v.append(tuple(c+rot@Vector((0,0,-radii[2]))))
    f=[(0,1+i,1+(i+1)%seg) for i in range(seg)]
    for j in range(rings-2):
        for i in range(seg):
            a=1+j*seg+i; b=1+j*seg+(i+1)%seg
            f.append((a,a+seg,b+seg,b))
    f.extend((bottom,1+(rings-2)*seg+(i+1)%seg,1+(rings-2)*seg+i) for i in range(seg))
    return v,f

def rod(a,b,r1,r2,seg=8):
    seg=max(seg,20)
    a,b=Vector(a),Vector(b); q=(b-a).to_track_quat('Z','Y')
    v=[]
    for c,r in [(a,r1),(b,r2)]:
        v.extend(tuple(c+q@Vector((r*math.cos(i*2*math.pi/seg),r*math.sin(i*2*math.pi/seg),0))) for i in range(seg))
    f=[tuple(reversed(range(seg))),tuple(range(seg,2*seg))]
    f.extend((i,(i+1)%seg,(i+1)%seg+seg,i+seg) for i in range(seg))
    return v,f

def combine(parts):
    vs=[]; fs=[]
    for v,f in parts:
        n=len(vs); vs.extend(v); fs.extend(tuple(i+n for i in face) for face in f)
    return vs,fs

def polygon_prism(points,y,depth,side):
    # points are CCW in XZ. Front +Y uses reversed winding.
    v=[(side*x,yy,z) for yy in (y-depth/2,y+depth/2) for x,z in points]
    n=len(points)
    f=[tuple(range(n)),tuple(reversed(range(n,2*n)))]
    f.extend((i,i+n,(i+1)%n+n,(i+1)%n) for i in range(n))
    if side<0: f=[tuple(reversed(t)) for t in f]
    return v,f

def materials():
    for name,h in COLORS.items():
        m=bpy.data.materials.get(name) or bpy.data.materials.new(name)
        srgb=[int(h[i:i+2],16)/255 for i in (0,2,4)]
        rgba=tuple(c/12.92 if c<=.04045 else ((c+.055)/1.055)**2.4 for c in srgb)+(1,)
        m.diffuse_color=rgba
        shader=next(n for n in m.node_tree.nodes if n.type=='BSDF_PRINCIPLED')
        shader.inputs['Base Color'].default_value=rgba
        shader.inputs['Roughness'].default_value=1
        shader.inputs['Metallic'].default_value=0

def phase1():
    if bpy.context.object and bpy.context.object.mode!='OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
    for o in list(bpy.data.objects): bpy.data.objects.remove(o,do_unlink=True)
    for blocks in [bpy.data.meshes,bpy.data.armatures,bpy.data.cameras,bpy.data.lights]:
        for block in list(blocks): blocks.remove(block)
    for c in list(bpy.data.collections): bpy.data.collections.remove(c)
    for a in list(bpy.data.actions): bpy.data.actions.remove(a)
    for m in list(bpy.data.materials): bpy.data.materials.remove(m)
    c=bpy.data.collections.new('Strawberry'); bpy.context.scene.collection.children.link(c)
    scene=bpy.context.scene; enum(scene.unit_settings,'system','METRIC')
    scene.render.fps=30; scene.frame_start=1; scene.frame_end=90
    cam=bpy.data.objects.new('cam_front',bpy.data.cameras.new('cam_front')); c.objects.link(cam)
    enum(cam.data,'type','ORTHO'); cam.data.ortho_scale=1.24
    cam.location=(0,3,.36); cam.rotation_euler=(Vector((0,0,.36))-cam.location).to_track_quat('-Z','Y').to_euler()
    scene.camera=cam
    light=bpy.data.objects.new('key_light',bpy.data.lights.new('key_light','AREA')); c.objects.link(light)
    light.location=(-1,2,3); light.rotation_euler=(Vector((0,0,.3))-light.location).to_track_quat('-Z','Y').to_euler()
    light.data.energy=160; light.data.size=3
    scene.world=bpy.data.worlds.new('Strawberry_world'); scene.world.color=(.10,.10,.10)
    scene.render.resolution_x=800; scene.render.resolution_y=600; scene.render.resolution_percentage=100
    enum(scene.render.image_settings,'file_format','PNG')
    scene.render.film_transparent=False
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type=='VIEW_3D':
                enum(area.spaces.active.region_3d,'view_perspective','CAMERA')
                enum(area.spaces.active.shading,'color_type','MATERIAL')
    materials()

def phase2():
    for o in list(bpy.data.collections['Strawberry'].objects):
        if o.type=='MESH': bpy.data.objects.remove(o,do_unlink=True)
    # Closed dome: six rings, one pole and a planar underside.
    v=[(0,0,.52)]; f=[]; seg=64; rings=18
    for j in range(1,rings+1):
        a=math.pi*.5*j/rings
        for i in range(seg):
            t=2*math.pi*i/seg
            v.append((.29*math.sin(a)*math.cos(t),.20*math.sin(a)*math.sin(t),.28+.24*math.cos(a)))
    f.extend((0,1+i,1+(i+1)%seg) for i in range(seg))
    for j in range(rings-1):
        for i in range(seg):
            a=1+j*seg+i; b=1+j*seg+(i+1)%seg
            f.append((a,a+seg,b+seg,b))
    f.append(tuple(reversed(range(1+(rings-1)*seg,1+rings*seg))))
    shell=mesh('mesh_shell',v,f,'mat_shell')
    shell.data.materials.append(bpy.data.materials['mat_shell_dark']); shell.data.polygons[-1].material_index=1
    make_belly()
    for side,s in [('L',1),('R',-1)]:
        base=(s*.12,.10,.40); eye=(s*.16,.12,.635)
        mesh('mesh_eyestalk_'+side,*rod(base,eye,.023,.021),'mat_shell_dark')
        eyegeo=ellipsoid(eye,(.050,.042,.052),16,8)
        pupil=ellipsoid((s*.16,.158,.639),(.019,.011,.024),12,6)
        ev,ef=combine([eyegeo,pupil]); ob=mesh('mesh_eye_'+side,ev,ef,'mat_eye')
        ob.data.materials.append(bpy.data.materials['mat_ink'])
        for p in list(ob.data.polygons)[len(eyegeo[1]):]: p.material_index=1
        ob['eye_center']=eye
        arm=rod((s*.23,.04,.37),(s*.365,.14,.435),.039,.05)
        upper=polygon_prism([(.345,.405),(.405,.395),(.515,.412),(.530,.465),(.490,.525),(.420,.545),(.355,.505)],.16,.095,s)
        mesh('mesh_claw_upper_'+side,*combine([arm,upper]),'mat_claw')
        lower=polygon_prism([(.363,.392),(.392,.350),(.462,.350),(.510,.384),(.515,.407),(.463,.390),(.410,.408)],.16,.08,s)
        ob=mesh('mesh_claw_lower_'+side,*lower,'mat_claw'); ob['hinge']=(s*.373,.16,.401)
        for i in range(1,4):
            a,b,c=leg_points(i,s)
            mesh('mesh_leg_'+side+str(i),*curved_leg(a,b,c),'mat_shell_dark')
    parts=[]
    for x,y,r in [(-.16,.095,.031),(-.065,.155,.029),(.05,.165,.032),(.16,.10,.031),(-.075,.04,.027),(.06,.065,.033),(.0,-.08,.030)]:
        z=.28+.24*math.sqrt(1-(x/.29)**2-(y/.20)**2)
        normal=Vector((x/.29**2,y/.20**2,(z-.28)/.24**2)).normalized()
        center=Vector((x,y,z))+normal*.003
        parts.append(ellipsoid(center,(r,r,.008),12,4,normal.to_track_quat('Z','Y').to_matrix()))
    mesh('mesh_spots',*combine(parts),'mat_cream')
    smooth_meshes()
    # All primitives created directly in world-space mesh data: explicit apply.
    for o in bpy.data.collections['Strawberry'].objects:
        if o.type=='MESH':
            active(o); bpy.ops.object.transform_apply(location=True,rotation=True,scale=True)

def phase3():
    materials()
    assert set(m.name for m in bpy.data.materials)==set(COLORS)

def phase4():
    old=bpy.data.objects.get('arm_strawberry')
    if old: bpy.data.objects.remove(old,do_unlink=True)
    data=bpy.data.armatures.new('arm_strawberry'); arm=bpy.data.objects.new('arm_strawberry',data)
    bpy.data.collections['Strawberry'].objects.link(arm); active(arm)
    bpy.ops.object.transform_apply(location=True,rotation=True,scale=True)
    bpy.ops.object.mode_set(mode='EDIT')
    defs={'root':((0,0,0),(0,0,.12),None),'body':((0,0,.33),(0,0,.48),'root')}
    for side,s in [('L',1),('R',-1)]:
        defs['eyestalk_'+side]=((s*.12,.1,.465),(s*.16,.12,.635),'body')
        defs['claw_arm_'+side]=((s*.23,.04,.37),(s*.365,.14,.435),'body')
    for name,(head,tail,parent) in defs.items():
        b=data.edit_bones.new(name); b.head=head; b.tail=tail
        if parent: b.parent=data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT'); arm.show_in_front=True
    for pb in arm.pose.bones: enum(pb,'rotation_mode','XYZ')
    for o in list(bpy.data.collections['Strawberry'].objects):
        if o.type!='MESH': continue
        bone='body'
        if o.name.startswith(('mesh_eye_','mesh_eyestalk_')): bone='eyestalk_'+o.name[-1]
        if o.name.startswith('mesh_claw_'): bone='claw_arm_'+o.name[-1]
        o.vertex_groups.clear(); group=o.vertex_groups.new(name=bone)
        group.add(list(range(len(o.data.vertices))),1,'REPLACE')
        for m in list(o.modifiers): o.modifiers.remove(m)
        mod=o.modifiers.new('rigid_bind','ARMATURE'); mod.object=arm
        o.parent=arm
    rigid_check()

def neutral():
    arm=bpy.data.objects.get('arm_strawberry')
    if arm:
        for pb in arm.pose.bones: pb.matrix_basis=Matrix.Identity(4)
    for o in bpy.data.collections['Strawberry'].objects:
        if o.type=='MESH' and o.data.shape_keys:
            for k in o.data.shape_keys.key_blocks: k.value=0
    bpy.context.view_layer.update()

def rigid_check():
    arm=bpy.data.objects['arm_strawberry']; neutral()
    for o in bpy.data.collections['Strawberry'].objects:
        if o.type!='MESH': continue
        assert all(len(v.groups)==1 and abs(v.groups[0].weight-1)<1e-7 for v in o.data.vertices)
    # Evaluate every point against its sole bone transform under combined rotation/scale.
    arm.pose.bones['claw_arm_L'].rotation_euler.z=.35
    arm.pose.bones['eyestalk_L'].rotation_euler.x=.25
    arm.pose.bones['body'].scale=(1.07,1.07,1.07)
    arm.pose.bones['body'].rotation_euler.y=.1
    bpy.context.view_layer.update(); dg=bpy.context.evaluated_depsgraph_get()
    for o in bpy.data.collections['Strawberry'].objects:
        if o.type!='MESH': continue
        name=o.vertex_groups[0].name
        mat=arm.pose.bones[name].matrix@arm.data.bones[name].matrix_local.inverted()
        ev=o.evaluated_get(dg)
        assert max((ev.data.vertices[v.index].co-mat@v.co).length for v in o.data.vertices)<1e-5,o.name
    neutral()

def phase5():
    for o in bpy.data.collections['Strawberry'].objects:
        if o.type=='MESH' and o.data.shape_keys: o.shape_key_clear()
    for name in ['mesh_shell','mesh_spots','mesh_belly']:
        o=bpy.data.objects[name]; o.shape_key_add(name='Basis'); key=o.shape_key_add(name='squash')
        for v in key.data: v.co=(v.co.x*1.05,v.co.y*1.05,.28+(v.co.z-.28)*.73)
    for side,s in [('L',1),('R',-1)]:
        make_eye_keys(bpy.data.objects['mesh_eye_'+side])
        o=bpy.data.objects['mesh_claw_lower_'+side]; o.shape_key_add(name='Basis')
        key=o.shape_key_add(name='claw_open_'+side); pivot=Vector(o['hinge']); rot=Matrix.Rotation(s*math.radians(48),3,'Y')
        for v in key.data: v.co=pivot+rot@(v.co-pivot)
    add_leg_morphs()
    add_sleep_morphs()
    for o in bpy.data.collections['Strawberry'].objects:
        if o.type=='MESH' and o.data.shape_keys: o.data.shape_keys.name='keys_'+o.name.removeprefix('mesh_')
    neutral()

def curve_list(action):
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                yield from bag.fcurves

def track_action(owner,action,slot,name,end):
    ad=owner.animation_data_create(); ad.action=None
    tr=ad.nla_tracks.new(); tr.name=name
    st=tr.strips.new(name,1,action); st.action_slot=slot
    st.action_frame_start=1; st.action_frame_end=end
    st.influence=1.0; st.use_animated_influence=False
    enum(st,'extrapolation','NOTHING'); enum(st,'blend_type','REPLACE')
    tr.mute=True

def phase6():
    arm=bpy.data.objects['arm_strawberry']
    owners=animated_owners()
    for o in owners: o.animation_data_clear()
    for a in list(bpy.data.actions): bpy.data.actions.remove(a)
    neutral()
    for name,end in CLIPS.items():
        clip_owners=[o for o in owners if o==arm or 'blink' not in o.key_blocks or name in {'sleep_enter','sleep_loop','wake_up'}]
        action=bpy.data.actions.new(name)
        # Layered action with armature + both squash-key slots; exactly six Actions.
        for owner in clip_owners:
            ad=owner.animation_data_create(); ad.action=action
            slot=action.slots.new(owner.id_type,owner.name); ad.action_slot=slot
        for frame in range(1,end+1):
            t=(frame-1)/(end-1); w=2*math.pi*t; neutral()
            pb=arm.pose.bones; breathe=0; tuck=0
            sleep_amount=0.0
            if name in {'sleep_enter','sleep_loop','wake_up'}:
                sleep_amount,breathe=sleep_pose(name,t)
                pb['body'].location.y=-.20*sleep_amount-.01566*breathe
                for side,sign in [('L',1),('R',-1)]:
                    pb['claw_arm_'+side].rotation_euler.x=-.40*sleep_amount
                    pb['eyestalk_'+side].rotation_euler.x=.18*sleep_amount
                    pb['eyestalk_'+side].rotation_euler.z=.10*sign*sleep_amount
                    pb['eyestalk_'+side].scale.y=1-.10*sleep_amount
            elif name=='idle_loop':
                pb['eyestalk_L'].rotation_euler.z=.045*math.sin(w)
                pb['eyestalk_R'].rotation_euler.z=-.045*math.sin(w+.4)
                pb['claw_arm_L'].rotation_euler.x=.025*math.sin(w)
                pb['claw_arm_R'].rotation_euler.x=-.025*math.sin(w)
                breathe=.22*(.5-.5*math.cos(w))
            elif name=='listen_loop':
                for side in ['L','R']:
                    pb['eyestalk_'+side].rotation_euler.x=-.12+.008*math.sin(w)
                pb['body'].location.y=.006
            elif name=='think_loop':
                pb['claw_arm_R'].rotation_euler.x=.16*(.5-.5*math.cos(3*w))
                pb['eyestalk_L'].rotation_euler.z=.12*math.sin(w)
                pb['eyestalk_R'].rotation_euler.z=.09*math.sin(w+.6)
            elif name=='talk_base': pb['body'].location.y=.004*(.5-.5*math.cos(w))
            elif name=='alert_snap':
                rise=min(1,t/.08); fall=max(0,min(1,(t-.66)/.34))
                v=rise*rise*(3-2*rise)*(1-fall*fall*(3-2*fall))
                for side in ['L','R']: pb['claw_arm_'+side].rotation_euler.x=.65*v
            elif name=='dance_loop':
                beat=8*w
                pb['body'].location.y=.015*(.5-.5*math.cos(beat))
                pb['body'].location.x=.018*math.sin(2*w)
                pb['body'].rotation_euler.z=.06*math.sin(2*w)
                pb['claw_arm_L'].rotation_euler.x=.12+.10*math.sin(4*w)
                pb['claw_arm_R'].rotation_euler.x=.12-.10*math.sin(4*w)
                pb['eyestalk_L'].rotation_euler.z=.035*math.sin(2*w)
                pb['eyestalk_R'].rotation_euler.z=-.035*math.sin(2*w)
                breathe=.08*(.5-.5*math.cos(beat))
            elif name=='notify_perk':
                hop,breathe,tuck,perk=notify_pose(t)
                pb['body'].location.y=hop
                pb['eyestalk_L'].rotation_euler.z=-.16*perk
                pb['eyestalk_R'].rotation_euler.z=.16*perk
                pb['claw_arm_L'].rotation_euler.x=.08*perk
                pb['claw_arm_R'].rotation_euler.x=.08*perk
            for p in pb:
                for prop in ['location','rotation_euler','scale']: p.keyframe_insert(prop,frame=frame,group=p.name)
            for owner in clip_owners[1:]:
                if 'blink' in owner.key_blocks:
                    if name in {'sleep_enter','sleep_loop','wake_up'}:
                        owner.key_blocks['blink'].value=sleep_amount
                        owner.key_blocks['blink'].keyframe_insert('value',frame=frame)
                else:
                    key='squash' if 'squash' in owner.key_blocks else 'leg_tuck'
                    value=breathe if key=='squash' else tuck
                    if name=='dance_loop' and key=='leg_tuck':
                        side=1 if '_L' in owner.name else -1
                        value=.45*max(0,side*math.sin(4*w))
                    owner.key_blocks[key].value=value
                    owner.key_blocks[key].keyframe_insert('value',frame=frame)
                    if 'sleep_fold' in owner.key_blocks:
                        owner.key_blocks['sleep_fold'].value=sleep_amount
                        owner.key_blocks['sleep_fold'].keyframe_insert('value',frame=frame)
        for fc in curve_list(action):
            for kp in fc.keyframe_points: enum(kp,'interpolation','LINEAR')
            if name.endswith('_loop') or name=='talk_base': assert abs(fc.evaluate(1)-fc.evaluate(end))<1e-6
        for owner in clip_owners:
            slot=owner.animation_data.action_slot
            track_action(owner,action,slot,name,end)
    neutral(); bpy.context.scene.frame_set(1)
    assert set(a.name for a in bpy.data.actions)==set(CLIPS)
    assert len(arm.animation_data.nla_tracks)==len(CLIPS)
    # Muted library tracks avoid accidentally stacking the six clips in Blender.

def preview(name):
    neutral()
    for owner in animated_owners():
        ad=owner.animation_data
        ad.action=None
        for tr in ad.nla_tracks:
            tr.mute=True
            if tr.name==name:
                ad.action=tr.strips[0].action
                ad.action_slot=tr.strips[0].action_slot
        owner.update_tag()
    bpy.context.scene.frame_end=CLIPS[name]; bpy.context.scene.frame_set(1)

def checkpoint(phase):
    scene=bpy.context.scene
    scene.render.filepath=str(OUT/f'phase_{phase:02d}.png')
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'strawberry_v2.blend'))
    bpy.ops.render.render(write_still=True)
    enum(scene.render.image_settings,"file_format","JPEG")
    scene.render.image_settings.quality=85
    bpy.data.images["Render Result"].save_render(str(OUT/f"phase_{phase:02d}.jpg"),scene=scene)
    enum(scene.render.image_settings,"file_format","PNG")
    print('CHECKPOINT',phase,scene.render.filepath)

def run_phase(n):
    globals()['phase'+str(n)](); checkpoint(n)

def build(n=6):
    for i in range(1,n+1): run_phase(i)


# V2 geometry and motion helpers. Executed after definitions, before build().
def curved_leg(a,b,c):
    a,b,c=Vector(a),Vector(b),Vector(c)
    pre=b+(a-b)*.18; post=b+(c-b)*.20
    centers=[]
    for i in range(7): centers.append(a.lerp(pre,i/7))
    for i in range(9):
        t=i/8; centers.append((1-t)**2*pre+2*(1-t)*t*b+t*t*post)
    for i in range(1,8): centers.append(post.lerp(c,i/7))
    vs=[]; fs=[]; segments=16
    for j,p in enumerate(centers):
        tangent=centers[min(j+1,len(centers)-1)]-centers[max(j-1,0)]
        q=tangent.to_track_quat('Z','Y')
        t=j/(len(centers)-1); radius=.026*(1-t)+.007*t
        for i in range(segments):
            angle=i*2*math.pi/segments
            vs.append(tuple(p+q@Vector((radius*math.cos(angle),radius*math.sin(angle),0))))
        if j:
            for i in range(segments):
                lo=(j-1)*segments+i; nex=(j-1)*segments+(i+1)%segments
                fs.append((lo,nex,nex+segments,lo+segments))
    fs.append(tuple(reversed(range(segments))))
    fs.append(tuple(range((len(centers)-1)*segments,len(centers)*segments)))
    return vs,fs

def smooth_meshes():
    add_lids()
    for obj in list(bpy.data.collections['Strawberry'].objects):
        if obj.type!='MESH': continue
        active(obj)
        for poly in obj.data.polygons: poly.use_smooth=True
        if obj.name=='mesh_shell':
            # Keep the underside flat while the dome shades continuously.
            obj.data.polygons[-1].use_smooth=False
        if obj.name.startswith(('mesh_eyestalk_','mesh_leg_')):
            for poly in obj.data.polygons:
                if len(poly.vertices)>4: poly.use_smooth=False
        add_basic_uvs(obj.data)

def add_leg_morphs():
    for side,sign in [('L',1),('R',-1)]:
        for index in range(1,4):
            obj=bpy.data.objects['mesh_leg_'+side+str(index)]
            obj.shape_key_add(name='Basis')
            key=obj.shape_key_add(name='leg_tuck',from_mix=False); key.slider_min=-.3
            base=Vector(leg_points(index,sign)[0])
            for vertex in key.data:
                delta=vertex.co-base
                vertex.co=base+Vector((delta.x*.76,delta.y*.86,delta.z*.62))

def animated_owners():
    return [bpy.data.objects['arm_strawberry']]+[
        obj.data.shape_keys for obj in bpy.data.collections['Strawberry'].objects
        if obj.type=='MESH' and obj.data.shape_keys and
        ('squash' in obj.data.shape_keys.key_blocks or 'leg_tuck' in obj.data.shape_keys.key_blocks or 'blink' in obj.data.shape_keys.key_blocks)]

def notify_pose(t):
    def ease(x): return x*x*(3-2*x)
    if t<.18:
        q=ease(t/.18)
        return -.008*q,.28*q,-.15*q,.15*q
    if t<.62:
        q=(t-.18)/.44
        rise=math.sin(math.pi*q)
        return .085*rise, .28*(1-ease(min(q*4,1))), .92*math.sin(math.pi*q)**.7, .15+.85*rise
    if t<.78:
        q=(t-.62)/.16; pulse=math.sin(math.pi*q)
        return -.008*pulse,.40*pulse,-.22*pulse,.12*(1-q)
    q=(t-.78)/.22
    return .003*math.sin(math.pi*q),0,0,0

exec(compile((OUT/"eye_claw_geometry.py").read_text(),"eye_claw_geometry.py","exec"))


def leg_points(index,side):
    # All six roots sit within the shell volume, with fore/mid/rear radial spacing.
    root_x=[.20,.23,.19][index-1];root_y=[.10,-.015,-.115][index-1]
    knee_y=[.19,-.04,-.205][index-1];toe_y=[.215,-.045,-.23][index-1]
    return ((side*root_x,root_y,.305),
            (side*(.34+.035*(3-index)),knee_y,.19-.040*(index-1)),
            (side*(.37+.038*(3-index)),toe_y,.055-.017*(index-1)))


def make_belly():
    # A shallow cream underside nested inside the shell rim, not a front bib.
    old=bpy.data.objects.get('mesh_belly')
    if old:
        bpy.data.objects.remove(old,do_unlink=True)
    obj=mesh('mesh_belly',*ellipsoid((0,.005,.278),(.244,.174,.058),48,16),'mat_cream')
    for polygon in obj.data.polygons: polygon.use_smooth=True
    return obj


def add_sleep_morphs():
    """Fold each leg close to its root, with the feet beside the resting belly."""
    for side,sign in [('L',1),('R',-1)]:
        for index in range(1,4):
            obj=bpy.data.objects['mesh_leg_'+side+str(index)]
            old=obj.data.shape_keys.key_blocks.get('sleep_fold')
            if old: obj.shape_key_remove(old)
            key=obj.shape_key_add(name='sleep_fold',from_mix=False)
            base=Vector(leg_points(index,sign)[0])
            for vertex,original in zip(key.data,obj.data.shape_keys.key_blocks['Basis'].data):
                delta=original.co-base
                vertex.co=base+Vector((delta.x*.24,delta.y*.65,delta.z*.17))

def sleep_pose(name,t):
    ease=lambda x: x*x*(3-2*x)
    q=1.0 if name=='sleep_loop' else ease(t if name=='sleep_enter' else 1-t)
    # Eight-second breath: gentle inhale, then longer relaxed exhale. Flat velocity at seams.
    if name=='sleep_loop':
        u=t/.42 if t<.42 else (1-t)/.58
        breathe=.22-.10*ease(u)
    else: breathe=.22*q
    return q,breathe
