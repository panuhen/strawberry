"""Reproducible Strawberry blockout. Run in Blender: exec(compile(open(...).read(),..., 'exec')); build(phase).
build(n) regenerates phases 1..n; run_phase(n) reruns a phase on its prerequisites.
Front is +Y; all mesh coordinates are baked into mesh data before rigging.

Headless, on the saved scene (model/README.md):
  blender -b --factory-startup model/strawberry_v2.blend --python model/build_strawberry.py -- --phase 4 --save --export widget/strawberry_v2.glb
"""
import bpy, math, json, os, stat, sys
from pathlib import Path
from mathutils import Vector, Matrix

# This file's folder (eye_claw_geometry.py lives beside it); checkpoint renders go to out_dir().
SRC = Path(globals().get('__file__') or os.path.expanduser('~/strawberry/model/build_strawberry.py')).resolve().parent
COLORS = {'mat_shell':'cf2b28','mat_shell_dark':'7d1516','mat_claw':'e2402f','mat_cream':'f6e3cf','mat_eye':'fbf3e8','mat_ink':'201318'}
PINCER_HINGE = (.373,.16,.401)   # the lower claw's hinge on her left (+X); mirrored on the right
PINCER_OPEN = math.radians(48)    # pincer_L/R rotation about its local X at full open (the old claw_open key)
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
        ob=mesh('mesh_claw_lower_'+side,*lower,'mat_claw'); ob['hinge']=(s*PINCER_HINGE[0],PINCER_HINGE[1],PINCER_HINGE[2])
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
    defs={'root':((0,0,0),(0,0,.12),None,None),'body':((0,0,.33),(0,0,.48),'root',None)}
    for side,s in [('L',1),('R',-1)]:
        defs['eyestalk_'+side]=((s*.12,.1,.465),(s*.16,.12,.635),'body',None)
        defs['claw_arm_'+side]=((s*.23,.04,.37),(s*.365,.14,.435),'body',None)
        # The pincer hinges at the lower claw's `hinge` point and lies along it. Its local X is the
        # hinge axis (front, +Y), so opening is a rotation about X: s*48° about +Y is fully open.
        hinge=Vector(PINCER_HINGE)*Vector((s,1,1))
        defs['pincer_'+side]=(hinge,hinge+Vector((s*.13,0,-.005)),'claw_arm_'+side,Vector((0,s,0)).cross(Vector((s,0,0))))
        for i in range(1,4):
            a,b,c=(Vector(p) for p in leg_points(i,s)); n=leg_normal(a,b,c)
            # Root to knee, knee to toe. Local X is the leg plane's normal: the knee's hinge axis.
            defs[f'leg_{side}{i}_upper']=(a,b,'body',n.cross((b-a).normalized()))
            defs[f'leg_{side}{i}_lower']=(b,c,f'leg_{side}{i}_upper',n.cross((c-b).normalized()))
    for name,(head,tail,parent,roll_z) in defs.items():
        b=data.edit_bones.new(name); b.head=head; b.tail=tail
        if roll_z is not None: b.align_roll(roll_z)
        if parent: b.parent=data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT'); arm.show_in_front=True
    for pb in arm.pose.bones: enum(pb,'rotation_mode','XYZ')
    for o in list(bpy.data.collections['Strawberry'].objects):
        if o.type!='MESH': continue
        for m in list(o.modifiers): o.modifiers.remove(m)
        o.vertex_groups.clear()
        if o.name.startswith('mesh_leg_'):
            skin_leg(o)
        else:
            bone='body'
            if o.name.startswith(('mesh_eye_','mesh_eyestalk_')): bone='eyestalk_'+o.name[-1]
            if o.name.startswith('mesh_claw_upper_'): bone='claw_arm_'+o.name[-1]
            if o.name.startswith('mesh_claw_lower_'): bone='pincer_'+o.name[-1]
            group=o.vertex_groups.new(name=bone)
            group.add(list(range(len(o.data.vertices))),1,'REPLACE')
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
        if o.name.startswith('mesh_leg_'):
            # Two bones per leg, weights summing to one; the root ring follows the upper bone alone.
            assert all(1<=len(v.groups)<=2 and abs(sum(g.weight for g in v.groups)-1)<1e-6 for v in o.data.vertices),o.name
            continue
        assert all(len(v.groups)==1 and abs(v.groups[0].weight-1)<1e-7 for v in o.data.vertices)
    for b in arm.data.bones:
        if b.name.startswith('leg_'):
            # The knee's hinge axis is the bone's local X (the gait and the clips rely on it).
            side=1 if '_L' in b.name else -1; i=int(b.name[5])
            a,k,c=(Vector(p) for p in leg_points(i,side))
            assert (b.matrix_local.to_3x3().col[0]-leg_normal(a,k,c)).length<1e-5,b.name
    # Evaluate every point against its sole bone transform under combined rotation/scale.
    arm.pose.bones['claw_arm_L'].rotation_euler.z=.35
    arm.pose.bones['pincer_L'].rotation_euler.x=.3
    arm.pose.bones['eyestalk_L'].rotation_euler.x=.25
    arm.pose.bones['body'].scale=(1.07,1.07,1.07)
    arm.pose.bones['body'].rotation_euler.y=.1
    bpy.context.view_layer.update(); dg=bpy.context.evaluated_depsgraph_get()
    for o in bpy.data.collections['Strawberry'].objects:
        if o.type!='MESH' or o.name.startswith('mesh_leg_'): continue
        name=o.vertex_groups[0].name
        mat=arm.pose.bones[name].matrix@arm.data.bones[name].matrix_local.inverted()
        ev=o.evaluated_get(dg)
        assert max((ev.data.vertices[v.index].co-mat@v.co).length for v in o.data.vertices)<1e-5,o.name
    neutral()
    # The pincer at full open is the old claw_open key exactly: s*48° about +Y through the hinge.
    for side,s in [('L',1),('R',-1)]:
        arm.pose.bones['pincer_'+side].rotation_euler.x=PINCER_OPEN
        bpy.context.view_layer.update(); dg=bpy.context.evaluated_depsgraph_get()
        o=bpy.data.objects['mesh_claw_lower_'+side]; ev=o.evaluated_get(dg)
        pivot=Vector(o['hinge']); rot=Matrix.Rotation(s*PINCER_OPEN,3,'Y')
        assert max((ev.data.vertices[v.index].co-(pivot+rot@(v.co-pivot))).length for v in o.data.vertices)<1e-5,o.name
        neutral()

def phase5():
    for o in bpy.data.collections['Strawberry'].objects:
        if o.type=='MESH' and o.data.shape_keys: o.shape_key_clear()
    for name in ['mesh_shell','mesh_spots','mesh_belly']:
        o=bpy.data.objects[name]; o.shape_key_add(name='Basis'); key=o.shape_key_add(name='squash')
        for v in key.data: v.co=(v.co.x*1.05,v.co.y*1.05,.28+(v.co.z-.28)*.73)
    for side in ['L','R']:
        make_eye_keys(bpy.data.objects['mesh_eye_'+side])
    # The pincers open with their bones (pincer_L/R) and the legs move with theirs: no claw or leg keys.
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
                # Snap up past the mark (about 10%) and settle, hold, then ease back down.
                v=back_out(t/.16)*(1-ease_in_out((t-.66)/.34))
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
            # The legs: each toe's target in the body's rest frame, reached by the two leg bones.
            for side,sign in [('L',1),('R',-1)]:
                lift=.45*max(0,sign*math.sin(4*w)) if name=='dance_loop' else tuck
                for i in range(1,4):
                    a,_,c=(Vector(p) for p in leg_points(i,sign))
                    toe=tuck_toe(a,c,lift)
                    if sleep_amount>0: toe=toe.lerp(fold_toe(a,c),sleep_amount)
                    pose_leg(arm,side,i,toe)
            for p in pb:
                for prop in ['location','rotation_euler','scale']: p.keyframe_insert(prop,frame=frame,group=p.name)
            for owner in clip_owners[1:]:
                if 'blink' in owner.key_blocks:
                    if name in {'sleep_enter','sleep_loop','wake_up'}:
                        owner.key_blocks['blink'].value=sleep_amount
                        owner.key_blocks['blink'].keyframe_insert('value',frame=frame)
                else:
                    owner.key_blocks['squash'].value=breathe
                    owner.key_blocks['squash'].keyframe_insert('value',frame=frame)
        # Every frame is a key: the motion's easing is in the pose functions above, and Bezier keys
        # with auto-clamped handles keep the curve smooth between them (glTF samples it per frame).
        for fc in curve_list(action):
            for kp in fc.keyframe_points:
                enum(kp,'interpolation','BEZIER'); enum(kp,'easing','AUTO')
                enum(kp,'handle_left_type','AUTO_CLAMPED'); enum(kp,'handle_right_type','AUTO_CLAMPED')
            fc.update()
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

def out_dir():
    """Where checkpoints go: $STRAWBERRY_BUILD_OUT, else the user's own cache dir
    ($XDG_CACHE_HOME or ~/.cache)/strawberry-build, created 0700. An existing one that is a symlink,
    not a directory, or another user's is refused: a shared path could be planted first."""
    override=os.environ.get('STRAWBERRY_BUILD_OUT')
    if override:
        path=Path(override).expanduser(); path.mkdir(parents=True,exist_ok=True)
        return path
    cache=os.environ.get('XDG_CACHE_HOME','')
    base=Path(cache) if os.path.isabs(cache) else Path(os.path.expanduser('~'))/'.cache'
    base.mkdir(parents=True,exist_ok=True)
    path=base/'strawberry-build'
    try: path.mkdir(mode=0o700)
    except FileExistsError: pass
    info=os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or (hasattr(os,'getuid') and info.st_uid!=os.getuid()):
        raise RuntimeError(f'{path} is a symlink, not a directory, or not yours; set STRAWBERRY_BUILD_OUT')
    return path

def checkpoint(phase):
    out=out_dir()
    scene=bpy.context.scene
    scene.render.filepath=str(out/f'phase_{phase:02d}.png')
    bpy.ops.wm.save_as_mainfile(filepath=str(out/'strawberry_v2.blend'))
    bpy.ops.render.render(write_still=True)
    enum(scene.render.image_settings,"file_format","JPEG")
    scene.render.image_settings.quality=85
    bpy.data.images["Render Result"].save_render(str(out/f"phase_{phase:02d}.jpg"),scene=scene)
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

def animated_owners():
    return [bpy.data.objects['arm_strawberry']]+[
        obj.data.shape_keys for obj in bpy.data.collections['Strawberry'].objects
        if obj.type=='MESH' and obj.data.shape_keys and
        ('squash' in obj.data.shape_keys.key_blocks or 'blink' in obj.data.shape_keys.key_blocks)]

def clamp01(x): return max(0.0,min(1.0,x))

def ease_in_out(x):
    """Cubic ease in and out: slow start, slow landing."""
    x=clamp01(x)
    return 4*x*x*x if x<.5 else 1-(-2*x+2)**3/2

def back_out(x,s=1.70158):
    """Ease out with a small overshoot (about 10% at s=1.7) that settles on 1."""
    x=clamp01(x)-1
    return 1+(s+1)*x*x*x+s*x*x

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
        return -.008*pulse,.40*pulse,-.22*pulse,.15*(1-ease(q))
    q=(t-.78)/.22
    return .003*math.sin(math.pi*q),0,0,0

exec(compile((SRC/"eye_claw_geometry.py").read_text(),"eye_claw_geometry.py","exec"))


def leg_points(index,side):
    # All six roots sit within the shell volume, with fore/mid/rear radial spacing.
    root_x=[.20,.23,.19][index-1];root_y=[.10,-.015,-.115][index-1]
    knee_y=[.19,-.04,-.205][index-1];toe_y=[.215,-.045,-.23][index-1]
    return ((side*root_x,root_y,.305),
            (side*(.34+.035*(3-index)),knee_y,.19-.040*(index-1)),
            (side*(.37+.038*(3-index)),toe_y,.055-.017*(index-1)))


LEG_RINGS = 16                     # vertices per ring of curved_leg; 23 rings root to toe
KNEE_BLEND = (9,13)                # rings over which a leg's weight passes from upper to lower
TUCK = Vector((.76,.86,.62))       # the old leg_tuck key: each toe pulled toward its root by this much
SLEEP_FOLD = (1.05,.9,-.09)        # asleep: toe at x, y of its rest offset, and this far below the root

def leg_normal(a,b,c):
    """The leg's plane: its normal is the knee's hinge axis (each leg bone's local X)."""
    return (b-a).cross(c-b).normalized()

def skin_leg(obj):
    """Rings up to the knee follow the upper bone, rings past it the lower one; the knee blends over
    KNEE_BLEND with smoothstep weights, so a bend stays round under the cel shading."""
    name='leg_'+obj.name.removeprefix('mesh_leg_')
    upper=obj.vertex_groups.new(name=name+'_upper'); lower=obj.vertex_groups.new(name=name+'_lower')
    assert len(obj.data.vertices)%LEG_RINGS==0
    lo,hi=KNEE_BLEND
    for v in obj.data.vertices:
        x=clamp01((v.index//LEG_RINGS-lo)/(hi-lo)); w=x*x*(3-2*x)
        if w<1: upper.add([v.index],1-w,'REPLACE')
        if w>0: lower.add([v.index],w,'REPLACE')

def tuck_toe(a,c,amount):
    """Where the old leg_tuck key put the toe at `amount` (negative stretches the leg)."""
    rel=c-a
    return a+Vector(tuple(rel[k]*(1+amount*(TUCK[k]-1)) for k in range(3)))

def fold_toe(a,c):
    rel=c-a
    return a+Vector((rel.x*SLEEP_FOLD[0],rel.y*SLEEP_FOLD[1],SLEEP_FOLD[2]))

def solve_leg(a,b,c,target):
    """Two-bone IK in the leg's rest frame: the knee for a toe at `target` (clamped to the leg's reach),
    bending the way it bends at rest. Returns (knee, toe)."""
    l1=(b-a).length; l2=(c-b).length
    d=target-a; dist=max(abs(l1-l2)+1e-4,min(l1+l2-1e-4,d.length)); dh=d.normalized()
    ac=(c-a).normalized(); pole=(b-a)-ac*(b-a).dot(ac)
    pole=(pole-dh*pole.dot(dh)).normalized()
    cos1=max(-1,min(1,(l1*l1+dist*dist-l2*l2)/(2*l1*dist)))
    knee=a+(dh*cos1+pole*math.sqrt(1-cos1*cos1))*l1
    return knee,a+dh*dist

def leg_frame(head,tail,n):
    y=(tail-head).normalized(); x=(n-y*n.dot(y)).normalized(); z=x.cross(y)
    m=Matrix((x,y,z)).transposed().to_4x4(); m.translation=head
    return m

def pose_leg(arm,side,index,target):
    """Pose leg `index` on `side` so its toe reaches `target` (body rest frame). The upper bone's parent
    is the body, so the body's own pose carries the whole leg."""
    sign=1 if side=='L' else -1
    a,b,c=(Vector(p) for p in leg_points(index,sign))
    up=arm.pose.bones[f'leg_{side}{index}_upper']; lo=arm.pose.bones[f'leg_{side}{index}_lower']
    if (target-c).length<1e-7:
        up.matrix_basis=Matrix.Identity(4); lo.matrix_basis=Matrix.Identity(4); return
    knee,toe=solve_leg(a,b,c,target)
    n=(knee-a).cross(toe-knee)
    n=n.normalized() if n.length>1e-6 else leg_normal(a,b,c)
    m_up=leg_frame(a,knee,n); m_lo=leg_frame(knee,toe,n)
    rest_up=up.bone.matrix_local; rest_lo=lo.bone.matrix_local
    up.matrix_basis=rest_up.inverted()@m_up
    lo.matrix_basis=rest_lo.inverted()@rest_up@m_up.inverted()@m_lo

def make_belly():
    # A shallow cream underside nested inside the shell rim, not a front bib.
    old=bpy.data.objects.get('mesh_belly')
    if old:
        bpy.data.objects.remove(old,do_unlink=True)
    obj=mesh('mesh_belly',*ellipsoid((0,.005,.278),(.244,.174,.058),48,16),'mat_cream')
    for polygon in obj.data.polygons: polygon.use_smooth=True
    return obj


def sleep_pose(name,t):
    ease=lambda x: x*x*(3-2*x)
    q=1.0 if name=='sleep_loop' else ease(t if name=='sleep_enter' else 1-t)
    # Eight-second breath: gentle inhale, then longer relaxed exhale. Flat velocity at seams.
    if name=='sleep_loop':
        u=t/.42 if t<.42 else (1-t)/.58
        breathe=.22-.10*ease(u)
    else: breathe=.22*q
    return q,breathe


def export_glb(path):
    """The widget's GLB: one glTF animation per clip (its Action), every channel kept at every frame,
    +Y up. Active actions are cleared first, or the exporter mixes the one left assigned (the last
    preview) into every clip's shape keys."""
    path=Path(os.path.expanduser(str(path)))
    for owner in animated_owners(): owner.animation_data.action=None
    neutral()
    bpy.ops.export_scene.gltf(filepath=str(path),export_format='GLB',export_yup=True,
        export_animation_mode='ACTIONS',export_optimize_animation_size=False)
    print('EXPORTED',path)


def main(argv):
    """`-- --phase N` reruns phases N..6 on the open scene, `--save` saves it, `--export PATH` writes the GLB."""
    import argparse
    parser=argparse.ArgumentParser(prog='build_strawberry.py')
    parser.add_argument('--phase',type=int)
    parser.add_argument('--save',action='store_true')
    parser.add_argument('--export')
    args=parser.parse_args(argv)
    if args.phase:
        for i in range(args.phase,7): globals()['phase'+str(i)]()
    if args.save: bpy.ops.wm.save_mainfile()
    if args.export: export_glb(Path(args.export).resolve())


if '--' in sys.argv and bpy.app.background:
    main(sys.argv[sys.argv.index('--')+1:])
