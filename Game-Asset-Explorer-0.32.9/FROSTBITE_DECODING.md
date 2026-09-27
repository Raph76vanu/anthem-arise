# Frostbite payload decoding

## External ChannelToDof maps and vector playback (0.27.14)

The installed EXM crouching/examine action-station RES carries the curve data
and an external Rigamate pointer, but no local `ChannelToDofAsset`. Its Eclipse
trailer key `4d3594ffcd5f4b79` resolves to a 129-entry ChannelToDof object inside
the shared Rigamate bank. The 89 rotations, 18 vectors, and 22 scalars exactly
account for those identifiers. Seventy-two rotation channels and four vector
channels name joints in the selected EXM render skeleton.

The player now applies mapped vector curves as absolute joint-local offsets and
samples every channel on the shared clip clock. Controller FPS and TimeScale
replace the former fixed three-second preview duration. Sequence transition
scheduling and scalar-driven procedural behavior remain separate work; the
program does not invent an additive blend where the assets do not establish one.

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

Version 0.23 replaces the experimental byte-pattern channel reader with a
bounded `EclipseAnimationAsset` decoder. It resolves exact ClipController names,
8- and 16-bit key-time formats, quantized float/vector values, dynamic
quaternion components, and associated `ChannelToDofAsset` identifiers. Testing
against the extracted Sentinel idle resource resolves three named clips with
internally consistent channel counts and timelines.

The first dynamic decoder incorrectly treated all three 16-bit words as direct
XYZ and reconstructed positive W. Later tests with four EXM clips showed
25.6–32.6% of six-byte keys cannot represent direct XYZ. Version 0.27.7
reads the low bit from each of the first two big-endian words as a two-bit
omitted-component selector (00=X, 01=Y, 10=Z, 11=W). The remaining 15/15/16
bits encode the three retained components across ±1/√2, in axis order, and
the positive missing component follows from unit length. Across four Outlaw
Ranger, six Lancer preview and 86 shared EXM clips, every one of over 130,000
dynamic keys passes the unit-quaternion bound check. Adjacent mapped-bone
rotations have no jumps above 60 degrees in the ten clips whose Rigamate bank
is verified. This supports the bit layout, while game-accurate composition and
constant/translation channels require visual verification. ChannelToDof values are opaque
identifiers owned by the referenced `BankPointer.Rigamate` bank; they cannot be
safely replaced by sequential SkeletonAsset indices or a curated joint-order
guess. The GUI exposes verified clips and curve counts but blocks posing until
that bank is found and decoded.

Version 0.24 decodes `BankPointerAsset` records themselves. In the real
Sentinel idle AntState, `BankPointer.ActionStation1` resolves to another local
GD.DATA object while `BankPointer.Rigamate [1]` exposes the external subject
key `cdb55c0a6ebc15cd`. The explorer now searches decoded installation RES
records by that key, starting with the owning and matching Javelin-family
bundles. A matching resource is reported with its virtual path and payload
size; an exhaustive or bounded miss is reported explicitly. This is dependency
resolution, not yet a claim that the bank's DOF-to-bone table is decoded, so
animation playback remains safely disabled until that last mapping is proven.

Version 0.25 retains a successfully resolved Rigamate payload for the active
animation and exposes **Extract Rigamate bank…** in the animation toolbar. It
writes the decoded RES and its archive descriptor as a pair, allowing the real
bank structure to be inspected outside the installation. This is the bridge
needed for the next decoder step; it still does not substitute sequential bone
indices or enable playback prematurely.

## Safety and scope

### EXM bind-rotation validation (0.27.9)

The supplied 182-joint EXM EBX has local and model bind-transform arrays.
When the stored 3x4 matrices are composed as `model[parent] * local[child]`,
all 181 child model transforms agree to within 0.000001 per matrix component.
The local rotation entries are column-major: the old quaternion extraction
treated each column as a row and therefore inverted the bind rotations on
128 nonidentity joints. The corrected quaternions now reproduce all 182
stored local rotations to within floating-point precision.

The Sentinel idle resource also embeds three referenced clip controllers in
the same sequence, and a Primary Rig Feature referring to a RigAsset in the
external EXM bank. Current playback does not yet implement every sequence
blend rule, apply every rig default channel, or evaluate scalar/procedural IK
channels. These are concrete remaining dependencies in the assets, so
appearance alone must not be used to certify the animation.

### Authored asset references (0.27.10)

Each sampled EXM EclipseAnimationAsset stores the key of its corresponding
ChannelToDofAsset 64 bytes before the end of its GD.DATA block. The local
key resolves in all three Sentinel idle clips, four Outlaw Ranger clips, and
six Lancer preview clips; two pairs reuse the same map. The decoder now joins
objects by those keys and leaves clips without a resolvable map unmapped.
It also extracts PrimaryRigFeatureAsset.Rig from each sampled animation
resource and verifies that key against the defining EXM RigAsset object in
the external bank before enabling its bone mapping. These checks establish
which assets belong together without making an assumption from file order,
channel count, or rig name. Sequence blending rules remain to be established
from the authored data. The 8-byte constant quaternion stores signed X/Y/Z in
three consecutive 21-bit fields spanning ±1/√2; bit 63 supplies W's sign.
Version 0.28.4 mistakenly consumed low component bits as selectors and folded
the Interceptor's spine and limbs. Version 0.28.5 restores those bits to their
component fields and keeps out-of-bound words explicitly invalid.

Version 0.26 decodes the EXM RigAsset `DofIds` array and its indexed
`RigDofSets`. The matching named DOF set records contain individual joint
channel names such as `Spine.q`, `Head.q`, and `LeftShoulder.q`. The Sentinel
idle sample maps all 127 clip IDs; 56 keyed rotation channels target joints
in the supplied EXM skeleton. The viewer now applies those curves as absolute
local joint rotations and skins weighted vertices. Same-camera contact sheets
show the old bank-delta composition collapsing the supplied Sentinel clips,
while absolute local rotations preserve a coherent upright skeleton at every
sampled phase. Named render-skeleton translations are applied as of 0.27.14;
scalar and auxiliary IK channels are not.

The explorer remains read-only. It does not include proprietary decompression
libraries, bypass ownership checks, contact game services, or modify the game.
Extracted assets remain subject to the game's licence and copyright.

### Sequence and rest-pose reference audit (0.27.11)

`audit_exm_pose.py` now follows the Sentinel idle sequence-player's three
indexed entries by their exact controller, blend-curve, and clip-initializer
keys. Their authored order is `Friendly_Idle`, `Friendly_Idle_Twitch9`, then
`Friendly_Idle_Twitch5`; the independent decoder returns the three Eclipse
curve clips in a different order. Each associated blend-curve object has a
distinct float (8, 17, 44 in that authored order). Its physical meaning and
the sequence's blend, scheduling, and layer policy are **not decoded**. Group
membership does not establish that Twitch is additive or that all three
clips play simultaneously. The audit never synthesizes a composite pose.

For the same 182-joint EXM EBX and authored EXM RigAsset, the audit compares
each mapped bank quaternion default with EBX local and accumulated world bind
rotations and their inverses. For the 69 mapped Sentinel Twitch9 joints the
local-bind median angle is 17.02 degrees (3 within one degree); inverse-local
29.97 (3); world-bind 111.77 (0); inverse-world 107.57 (0). The Hips default
differs from its local bind by 120 degrees. Those are measured comparisons,
not a conversion formula: the bank's animation rest and the mesh's skin bind
can be distinct. Version 0.27.13's comparison sheet rejects the former
`inverse(bank_default) * sampled_curve` then `bind * delta` path: it collapses
the skeleton in every sampled phase, whereas using the curve as the absolute
local rotation gives a coherent figure. The 13 mapped constant rotations are
applied, but all 19 vector channels remain omitted for Twitch9. The
user-facing playback is therefore still experimental and cannot be certified
from the available invariants. Run the audit on your extracted resource, EBX,
and bank to see the resolved references and per-bone checks in JSON and Markdown.
