# Anthem EXM Lancer detail findings

## Base MeshSet streamed detail

Anthem keeps the highest-detail streamed chunks in a global table in each
super-bundle TOC. They are not ordinary entries in the bundles' `chunks`
arrays. Parsing the Anthem-specific table resolves the base MeshSets' own LOD0,
LOD1, and LOD2 GUIDs to their CAS records.

The inspected installation now resolves LOD0 through LOD5 for all four
playable base MeshSets. Direct LOD1 decode produced fully weighted geometry:

- Lancer: 37,785 vertices, 38,596 triangles.
- Interceptor: 23,648 vertices, 28,389 triangles.
- Colossus: 37,516 vertices, 39,332 triangles.
- Storm: 19,744 vertices, 25,095 triangles.

## Separate cosmetic geometry

The playable Javelin is represented by separate customization MeshSets too:

- `exm_lancer_base_arms_model_mesh`: LOD1–LOD5; LOD1 has 1,478 weighted vertices.
- `exm_lancer_base_legs_model_mesh`: LOD1–LOD5; LOD1 has 264 weighted vertices.
- `exm_lancer_base_torso_model_mesh`: LOD1–LOD5; LOD1 has 2,099 weighted vertices.

All three LOD1 pieces decode with valid skin weights. Version 0.27.23 assembles
matching modular pieces by family and variant, and keeps the complete set on a
common-LOD detail slider for animation playback.

`exm_lancer_bitpack_spacemarine_model_mesh` is a separate, complete five-part
LOD0 model stored in a Fortress of Dawn bundle. It decodes to 84,139 vertices
and 106,798 triangles across legs, body, torso, arms, and helmet materials.
This record has no skin streams and is stored in an already posed form, so it
is useful proof and a high-detail static preview, not a drop-in replacement for
the currently animated base MeshSet.

## UI consequence

The viewer's `LOD info…` button reports declared, resolved, and unresolved
levels. The base model's detail slider now directly selects LOD5 through LOD0.
Related modular MeshSets and their optional assembly button are explicitly
labeled cosmetic/customization content so they are not confused with the base
model's own high-detail levels.
