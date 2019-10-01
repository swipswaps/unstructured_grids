# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 3
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

# <pep8 compliant>

# Operations related to extrusion of new cells

import bpy
from . import ug
from . import ug_op
import logging
l = logging.getLogger(__name__)
fulldebug = False # Set to True if you wanna see walls of logging debug

class UG_OT_ExtrudeCells(bpy.types.Operator):
    '''Extrude new cells from current face selection'''
    bl_idname = "unstructured_grids.extrude_cells"
    bl_label = "Extrude Cells (UG)"

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'EDIT_MESH'}

    def execute(self, context):
        # Initialize from selected faces if needed
        initialization_ok, initial_faces = initialize_extrusion()
        if not initialization_ok:
            self.report({'ERROR'}, "Initialization failed. Maybe " \
                        + "no faces were selected, or object name is " \
                        + "%r?" % ug.obname)
            return {'FINISHED'}

        # Layer extrusion
        ug_props = bpy.context.scene.ug_props
        n = 0 # new cell count
        vdir = dict() # Extrusion direction dictionary, updated per layer

        for i in range(ug_props.extrusion_layers):
            nf, vdir = extrude_cells(initial_faces, vdir)
            n += nf
            initial_faces = [] # Clear, only used for first layer
            if n == 0:
                self.report({'ERROR'}, "No object %r" % ug.obname)
                return {'FINISHED'}

        self.report({'INFO'}, "Extruded %d new cells" % n)
        return {'FINISHED'}


def initialize_extrusion():
    '''Initialize UG data for extrusion. For a new unstructured grid,
    create UG object and UGFaces from faces of active object.
    Return values are boolean for successful initialization and
    list of initial faces (if initializing from faces when no cells exist).
    '''

    initial_faces = [] # List of new UGFaces

    # Do nothing if there is already an UG state
    if ug.exists_ug_state():
        return True, initial_faces

    source_ob = bpy.context.active_object
    if source_ob.name == ug.obname:
        return False, initial_faces

    # Mode switch is needed to make sure mesh is saved to original object
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.mode_set(mode='EDIT')
    ob = ug.initialize_ug_object()

    import bmesh
    bm = bmesh.from_edit_mesh(source_ob.data)

    # Delete unselected faces
    facelist = []
    for f in bm.faces:
        if f.select == False:
            facelist.append(f)
    bmesh.ops.delete(bm, geom=facelist, context='FACES_ONLY')

    # Delete leftover verts which are not part of any faces
    vertlist = []
    for v in bm.verts:
        if len(v.link_faces) == 0:
            vertlist.append(v)
    bmesh.ops.delete(bm, geom=vertlist, context='VERTS')

    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    # Bail out if no faces are left
    if len(bm.faces) == 0:
        ug.delete_ug_object()
        return False, initial_faces

    # Generate ugverts
    for i in range(len(bm.verts)):
        ug.UGVertex(i)
    l.debug("Initial vertex count: %d" % len(ug.ugverts))

    # Generate ugfaces
    for i in range(len(bm.faces)):
        verts_ind = [v.index for v in bm.faces[i].verts]
        uf = ug.UGFace(verts_ind)
        uf.bi = i
        initial_faces.append(uf)
    l.debug("Initial Face count: %d" % len(ug.ugfaces))

    bm.to_mesh(ob.data)
    bm.free()

    # Hide everything else than UG object
    bpy.ops.object.mode_set(mode = 'OBJECT')
    ug.hide_other_objects()
    bpy.ops.object.mode_set(mode = 'EDIT')

    return True, initial_faces


def extrude_cells(initial_faces, vdir):
    '''Extrude new cells from current face selection. Initial faces
    argument provides optional list of initial UGFaces whose direction
    is reversed at the end. vdir is the dictionary for extrusion
    directions.
    '''

    import bmesh
    ob = ug.get_ug_object()
    bm = bmesh.from_edit_mesh(ob.data)
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    # Get selected faces
    faces = [f for f in bm.faces if f.select]
    l.debug("Face count: %d" % len(faces))


    def calculate_extrusion_dir_and_coeffs(verts):
        '''Calculate normalized extrusion direction vector and length
        coefficients based on angles of surrounding faces. Return
        dictionaries of directions and coeffs for each argument mesh
        vertex.
        '''

        from mathutils import Vector
        vdir = dict() # extrusion directions to be calculated
        coeffs = dict() # extrusion length coefficients to be calculated
        ug_props = bpy.context.scene.ug_props

        for v in verts:
            # Default extrusion direction is calculated as average of
            # surrounding face normal vectors.
            n = 0
            vec = Vector((0, 0, 0))
            for f in v.link_faces:
                fi = f.index
                uf = ug.get_ugface_from_face_index(fi)
                if uf.deleted:
                    continue
                if uf.neighbour != None:
                    continue
                if ug_props.extrusion_ignores_unselected_face_normals:
                    if f.select == False:
                        continue
                vec += f.normal
                n += 1
            vdir[v.index] = vec / float(n)

            # Length coefficient, TODO
            coeffs[v.index] = 1.0

        return vdir, coeffs


    def cast_vertices(bm, faces, vdir):
        '''Create new vertices from vertices of faces in argument bmesh, by
        casting each vertex towards initial (vdir) or updated average
        face normal direction. Return updated bmesh and vertex mapping
        dictionary, and initial extrusion direction dictionary.
        '''

        orig_verts = [] # List of vertices from which to cast new verts
        vert_map = {} # Dictionary for mapping original face verts to new verts
        ind = bm.verts[-1].index # Index of last vertex
        ug_props = bpy.context.scene.ug_props

        # Extrusion length
        extrude_len = ug_props.extrusion_thickness

        # Find the original verts
        for f in faces:
            for v in f.verts:
                if v in orig_verts:
                    continue
                orig_verts.append(v)

        # Calculate updated extrusion direction and length
        # coefficients for vertices based on current face normals
        new_vdir, coeffs = calculate_extrusion_dir_and_coeffs(orig_verts)

        # Cast new vertices
        save_vdir = dict() # saved direction vector for next layer
        for v in orig_verts:
            if ug_props.extrusion_uses_fixed_initial_directions and v.index in vdir:
                vertdir = vdir[v.index]
            else:
                vertdir = new_vdir[v.index]

            newco = v.co + extrude_len * vertdir * coeffs[v.index]
            v2 = bm.verts.new(newco)
            vert_map[v] = v2
            # Create new UGVertex
            ind += 1
            uvert = ug.UGVertex(ind)
            # Map vdir for next round
            save_vdir[ind] = vertdir

        bm.verts.ensure_lookup_table()
        bm.verts.index_update()

        # Update layer thickness using expression by user
        x = extrude_len
        expr = ug_props.extrusion_scale_thickness_expression
        try:
            rval = eval(expr)
            l.debug("Expression returned %s" % str(rval))
            ug_props.extrusion_thickness = float(rval)
        except:
            l.error("Error in evaluating: %r" % expr)

        return bm, vert_map, save_vdir

    bm, vert_map, vdir = cast_vertices(bm, faces, vdir)

    def point_neighbour_cell_to_internal_face(e, nc, nf0, bm, vert_map):
        '''Help function to set neighbour cell of the face which is extruded
        from edge e (old face) to point to UGCell index nc. This
        function is called for faces which have been already extruded
        and have an owner, so neighbour cell needs only to point to
        that face.
        '''

        # First find the existing extruded mesh face index (efi) from
        # edge e. Face index is searched after nf0.
        e0 = e.verts[0]
        e1 = e.verts[1]
        verts = [e0, vert_map[e0], vert_map[e1], e1]
        bm.faces.ensure_lookup_table()

        test = True
        fi = -1 # mesh face index
        found = False
        for i in range(nf0, len(bm.faces)):
            test = True
            for v in verts:
                if v not in bm.faces[i].verts:
                    test = False
                    break
            if test:
                fi = i
                found = True
                break

        if not found:
            l.error("Sanity violation: Did not find extruded face")
            return False

        if fulldebug: l.debug("fi %d, nc %d" % (fi, nc))

        # Set the neighbour cell
        new_cell = ug.ugcells[nc]
        old_face = ug.get_ugface_from_face_index(fi)
        old_face.neighbour = new_cell
        # Add face to cell faces
        new_cell.add_face_info(old_face)
        return True

    def create_faces(bm, faces, vert_map):
        '''Main extrusion function. Create faces to boundary sides and top of
        extrusion.
        '''

        nc = len(ug.ugcells) # Index number of cells
        nf0 = len(bm.faces) # Initial index number of mesh faces
        nf = 0 # Number of faces created
        processed_edges = [] # List of processed edges
        newfaces = [] # List of new UGFaces

        # TODO: Bulky function, refactor to smaller pieces

        # Create a new UGCell for each extruded face
        for f in faces:
            ug.UGCell()
        if fulldebug: l.debug("Cell count: %d" % len(ug.ugcells))

        # Extrusion tasks are done for each new cell, whose extrusion
        # base face is f.
        for f in faces:
            new_cell = ug.ugcells[nc]

            # 1. Extrude faces from base face edges

            for e in f.edges:
                # For existing (internal) faces, add only neighbour cell info
                if e in processed_edges:
                    test = point_neighbour_cell_to_internal_face(e, nc, nf0, bm, vert_map)
                    if not test:
                        return None, None
                    continue
                processed_edges.append(e)

                # For new faces, create new face to bmesh
                e0 = e.verts[0]
                e1 = e.verts[1]
                verts = [e0, vert_map[e0], vert_map[e1], e1]
                f2 = bm.faces.new(verts)
                f2.normal_update()

                # Flip face normal if face normal points inside of new cell.
                # Uses edge center to original face center as reference vector.
                # Cells are required to be convex for this to work correctly.
                edgevec = 0.5*(e0.co+e1.co)
                refvec = f.calc_center_median() - edgevec
                refvec.normalize()
                cos_epsilon = f2.normal @ refvec
                if fulldebug:
                    l.debug("f2.normal:%s, refvec:%s" %(str(f2.normal), str(refvec)))
                    l.debug("cos_epsilon = %f" % cos_epsilon)
                if (cos_epsilon > 0.0):
                    f2.normal_flip()
                    f2.normal_update()
                    if fulldebug: l.debug("Flipped face normal")

                # Create UGFace
                verts_ind = [x.index for x in f2.verts]
                if fulldebug: l.debug("Vertex indices: %s" % str(verts_ind))
                uf = ug.UGFace(verts_ind)
                uf.bi = len(bm.faces) - 1
                newfaces.append(uf)
                uf.owner = new_cell
                new_cell.add_face_info(uf)
                nf += 1

            # 2. Add new cell information to original base face.
            # Base face is always set as neighbour. If base face is
            # not originally part of any cell, it's face normal will
            # be flipped later on, and new cell becomes owner then.
            orig_face = ug.get_ugface_from_face_index(f.index)
            orig_face.neighbour = new_cell
            if fulldebug: l.debug("Set face %d owner to %d" %(f.index, new_cell.ii))
            new_cell.add_face_info(orig_face)

            # 3. Create face at top of extrusion

            topverts = []
            for i in f.verts:
                topverts.append(vert_map[i])
            ftop = bm.faces.new(topverts)

            # Create UGFace
            verts_ind = [x.index for x in ftop.verts]
            if fulldebug: l.debug("Top face vertex indices: %s" % str(verts_ind))
            uf = ug.UGFace(verts_ind)
            uf.bi = len(bm.faces) - 1
            newfaces.append(uf)
            uf.owner = new_cell
            # Add UGFace (and it's UGVerts) to UGCell
            new_cell.add_face_info(uf)
            nf += 1

            # 4. Finishing. Select new faces and deselect original faces

            f.select_set(False)
            ftop.select_set(True)
            nc += 1

        l.debug("New faces created: %d" % nf)
        return bm, newfaces

    bm, newfaces = create_faces(bm, faces, vert_map)

    # Reverse direction of initial faces (first extrusion only)
    bm.faces.ensure_lookup_table()
    for f in initial_faces:
        l.debug("Final flipping face %d" % f.bi)
        bm.faces[f.bi].normal_flip()
        bm.faces[f.bi].normal_update()
        f.invert_face_dir()

    # Finish up
    bm.normal_update()
    bmesh.update_edit_mesh(mesh=ob.data)
    bm.free()
    bpy.ops.object.mode_set(mode = 'OBJECT')
    bpy.ops.object.mode_set(mode = 'EDIT')
    ug_op.set_faces_boundary_to_default(newfaces)
    ug.update_ug_all_from_blender()

    return len(faces), vdir
