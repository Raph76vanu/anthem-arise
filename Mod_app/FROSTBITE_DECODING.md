# Frostbite payload decoding

Game Asset Explorer identifies Frostbite installations by their file structure,
not by a game-specific switch. The built-in reader follows the container layer:

- `.toc` and `.sb` describe bundles and resource records.
- `.cat` files map content hashes.
- `.cas` files contain the compressed payload bytes.

A container is not itself a mesh. Producing a renderable model requires a
decoder profile compatible with that Frostbite data generation, followed by
four real operations: resolve the TOC/SB record, read the referenced CAS range,
decompress it, and interpret the resource schema plus its chunk data.

## Anthem and Javelin models

Anthem commonly uses Oodle-compressed CAS blocks. A legal local extraction must
load the matching 64-bit `oo2core_*_win64.dll` shipped with the installed game;
the DLL is not redistributed with this project. The open-source
[AnthemTool](https://github.com/xyrin88/anthemtool) can
resolve and decompress Anthem EBX, RES, and chunk records. Its raw MeshSet
output contains the resource declaration; its external chunk contains the
vertex and index buffers. Version 0.16 includes an Anthem-generation MeshSet
interpreter for both loose decoded resource/chunk pairs and records stored in a
complete installed game's TOC/SB/CAS layout.
[Frosty Tool Suite](https://github.com/FrostyToolsuite/FrostyToolsuite) contains
game-profile and MeshSet infrastructure; compatible builds/plugins can export
meshes as OBJ/FBX.

To preview a decoded pair:

1. Put `name.meshset` and `name_lodN.chunk` in the same folder.
2. Scan that folder in Game Asset Explorer.
3. Select the `.meshset` row and click **Preview**. The reader chooses an
   available LOD, follows its section/stream declarations, and excludes the
   duplicate depth and shadow sections.

When a complete Anthem installation is selected, MeshSet RES entries are
catalogued automatically. Preview reads only the selected MeshSet and its first
available low-detail chunk, verifies their compressed hashes, uses the locally
installed Oodle DLL when required, and leaves every game archive unchanged.

The decoder supports the declarations observed in the supplied Anthem
Javelin resources: 16-bit triangle-list indices and Float3/Float4 or
Half3/Half4 positions. Unsupported declarations stop with a bounded message.
The adapter is format-generation-specific rather than selected by game title:
other Frostbite generations and Frostbite textures, animations, and rigs need
their matching schemas. Decoded MeshSets are published through the same neutral
`AssetRecord` interface used by Unity and RAGE readers.

Version 0.19 also follows the paired bone-index and skin-weight declarations in
each visible section and reads their four raw values for every vertex. The Rig
Inspector reports weighted-vertex coverage, stream numbers, weight sums, and
the local palette slots referenced by the mesh. It also decodes each section's
bone palette and maps those slots to numeric skeleton bone IDs. The IDs are not
presented as named bones: the matching `*_master_skeleton.ebx` hierarchy and
bind pose still have to be decoded before safe deformation or animation
playback is possible.

Version 0.20 reads the schema-independent header of Frostbite EBX files. Anthem's
named `*_master_skeleton.ebx` can be a tiny alias containing an external EBX
pointer instead of the hierarchy itself. Skeleton extraction now resolves that
file GUID against the owning bundle first, then the remaining installation
metadata, and saves the linked payload and descriptor beside the alias.

Version 0.21 decodes the linked Anthem `SkeletonAsset`: its bone-name array,
parent indices, local pose, and model-space bind pose are identified
and the local/model pair is accepted only when parent-child distances satisfy the
decoded hierarchy. A Javelin MeshSet is matched to its `exa`, `exh`, `exi`, or
`exm` master skeleton, palette bone IDs are bounds-checked, and the validated
model-space bind hierarchy can be toggled over the mesh with **Bones**. This is
bind-pose visualization; animation clip decoding and playback are still future
work.

Version 0.22 keeps the complete four-bone/four-weight binding for every decoded
vertex and adds a persistent active-character state. **Use for animation** locks
the mesh, bind skeleton, palette mapping, and weights while the user browses the
Animations category. Selected animation-path EBX/RES payloads are now decoded
from CAS and structurally inspected; family hints, EBX imports, and array counts
are shown. Play remains deliberately disabled until an actual Anthem compressed
keyframe payload and its channel/time encoding have been identified and decoded.

## Safety and scope

The explorer remains read-only. It does not include proprietary decompression
libraries, bypass ownership checks, contact game services, or modify the game.
Extracted assets remain subject to the game's licence and copyright.
