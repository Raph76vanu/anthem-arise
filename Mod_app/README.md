# Game Asset Explorer 0.27.7

A read-only, cross-game asset discovery tool for legitimately obtained game files.
It scans a game directory, identifies common engine/container layouts, groups likely
character meshes with nearby rigs, animations, materials and textures, ranks the
groups, and renders supported geometry in an interactive viewport inside the program.

## What version 0.27.7 can do

**EXM rotation decoding:** dynamic six-byte keys now read the two low selector
bits, restore the omitted quaternion component, and decode the remaining
three values in their 15/15/16-bit ranges. Tests on the supplied four Outlaw
Ranger clips, six Lancer preview clips and 86 shared EXM clips find no keys
outside unit-quaternion bounds. All ten clips with resolved EXM bone mappings
have no adjacent mapped-bone rotation jumps above 60 degrees. This replaces
the old direct XYZ rule, whose raw components exceeded unit length in
25.6–32.6% of the four Outlaw Ranger clips. The diagnostic PNGs compare the
updated playback with that old rule. Please inspect an actual mesh and clip:
mathematical and timing checks cannot establish game-accurate posing by
themselves.

**EXM animation diagnostics:** double-click `diagnose_exm_animation.bat`,
choose an extracted animation `.res`, matching `exm_skeleton.ebx`, and the
resolved Rigamate bank `.res`. The `animation_diagnostics` folder gets a
comparison PNG per clip, `measurements.csv`, and a short explanation.
Alternatively, run `python diagnose_exm_animation.py ANIMATION.res
exm_skeleton.ebx BANK.res OUTPUT_FOLDER` from a terminal. This is a separate
experiment: it does not modify the game's files or change animation playback.
Rows compare the current rule to the previous XYZ rule, composition,
bank-default, and half-strength hypotheses. All pictures for a given clip use one camera
and scale. A low displacement score is not evidence of the correct codec.
The supplied four Outlaw Ranger clips share the same complete frame range,
so changing between per-channel and shared-clip time would make no difference.

The animation **Clip** selector now includes an explicit clip counter and
**Previous / Next** buttons. For a record containing four Eclipse clips, the
counter runs from `1 / 4` to `4 / 4`, and changing clips also changes the
bank-mapped bone rotations selected for playback. Choosing another clip during
playback stops the old one so **Play** starts the newly selected clip.

To check EXM bone motion independently of skinning, select and preview the
Lancer mesh, enable **Use for animation**, then select the Patch Outlaw Ranger
animation RES in **Animations**. Choose an `EXM_SCAV_EXP_2h_Flyer_Idle_Twitch`
clip and click **Play**. Uncheck **Mesh** in the viewport to show only its moving
bone overlay; recheck it to compare the animated geometry. Bone-only playback
skips CPU mesh skinning and rendering, which helps on slower laptops. The four
provided clips resolve 41–64 named rotations against the 182-joint EXM skeleton.
They belong to an enemy animation set: sharing the EXM rig allows this mapping
experiment, but does not establish that they are player Lancer animations.
Quaternion layout has strong cross-file evidence; pose composition, omitted
constant rotations and root motion still need visual verification.

Some Anthem Eclipse animations contain no float channels. The decoder now
accepts the verified zero-float descriptor and reads the remaining position
and rotation curves. The supplied EXM player shared bundle decodes all 86
animation objects, whereas seven previously caused an invalid channel count
error. This RES contains no clip controller names, ChannelToDof mapping, or
Rigamate pointer, and its supplied EBX contains only a resource reference.
Those unnamed curves can be inspected, but their bone assignments cannot yet
be verified for playback. The preview explains this limitation directly.

Use **Exclude** beside **Search** to hide results containing any of the
entered words. Search requires every word; Exclude rejects a result if any
word appears in its archive path, internal path, bundle name, or indexed clip
name. Both filters work across asset categories and inside folder-only views.
For example, search `EXM` and exclude `enemies` to hide records whose path
contains the `enemies_bgc` folder. The animation clip selector now occupies
its own full-width row so complete clip names are easier to inspect.

Animations now include an opt-in **Search clip names** filter. When enabled,
the explorer indexes names embedded inside Frostbite `.res` animation records
in the background and searches those names alongside paths. Queries such as
`idle`, `walk`, `run`, or `flight` can therefore find a matching sub-animation
inside a generic archive; selecting the result still opens the full record and
its clip selector.

Lancer player-preview clips in the generic `exo/playerpreview` directory now
identify their EXM rig from their verified `exm_lancer_` filename. The generic
`exo` directory is no longer mistaken for a Javelin family; a mismatched
filename remains incompatible. With the supplied EXM Rigamate bank and
182-bone skeleton, `EXM_SuitClose` maps 38 animated bone rotations and can
reach the experimental Play control. Suit interaction clips are not necessarily
useful for running or flying, or fully visible on the base Javelin mesh.

Anthem Eclipse animation channel tables now use their stored array offsets.
Some Lancer player-preview clips add alignment padding between float and
vector channels; previously this caused "An Eclipse key-time range points
outside its stream". The supplied Lancer RES now decodes all six clips:
`EXM_SuitClose`, `EXM_Suit_EnterEXM_exm_Lancer`, `EXM_SuitOpen_Idle`,
`EXM_Suit_ExitEXM_01_Lancer`, `EXM_SuitOpen`, and `EXM_SuitClose_Idle`.
These clips represent suit interactions and suit-state idles, and are not
verified as full-body standing idle animations. Visual posing still depends
on a compatible resolved Rigamate bank and the experimental rotation decoder.

In **Animations**, check **.res only** to hide `.ebx` metadata records. This
filters the existing inventory before folder grouping, including within
folder-only and virtual-folder views. Equipment subfolders such as `beam` no
longer override the EXM Lancer family when evaluating animation candidates.
Matching a family alone does not prove that a clip is suitable for the full
body or that the packed rotation data has been decoded correctly.

Earlier versions tried swapping XYZ values to smooth apparent animation
jumps. The 48-bit decoder now reads the stored omitted-component selector,
so the preview no longer applies that swap heuristic. The remaining pose may
still be wrong if bank defaults, constant channels or root motion are missing.

When the matching EXM Rigamate resource is available, the animation preview
matches channel DOF IDs to named DOF sets and then to the selected EXM skeleton.
It can play decoded rotation curves and skin the mesh using its four stored bone
weights per vertex. The sample Sentinel idle clip resolves 56 moving rotations
to its supplied 182-bone skeleton; helper joints outside that skeleton are
skipped. Rotations are currently interpreted relative to the bank's default
pose. This composition is experimental; visual accuracy, constant channels,
root translation and non-rotation curves still need work. On slower machines,
CPU skinning and software rendering may reduce frame rate.

- Scan loose files without modifying the game installation.
- Recognize common mesh, skeleton, animation, material and texture extensions.
- Identify Unity, Unreal Engine and Frostbite container layouts.
- Detect Rockstar RAGE/GTA IV installations and character archives.
- Group related assets using filenames, directories, LOD suffixes and character terms.
- Rank likely character bundles and select the highest-confidence result.
- Export a complete JSON report.
- Preview OBJ, STL, and experimental GTA IV WDR/WDD geometry inside the app.
- Rotate with left-drag, zoom with the mouse wheel, toggle wireframe, and reset view.
- Use coalesced low-latency interaction frames and automatically refine after dragging stops.
- Shade faces with ambient, key, and fill lighting so their form remains visible without textures.
- Filter scan results by Characters / meshes, Animations, Textures, Props, Images,
  Map parts, Skeletons, Weapons & carryables, or Containers / archives.
- Search the current category live by internal asset name, archive path, or
  Frostbite bundle name. Large inventories are filtered with a short debounce.
- Scan a specific nested game folder without losing access to engine metadata.
  The chosen folder remains the visible result scope, while Frostbite readers
  automatically locate the nearest parent installation layout and shared CAS
  files. MeshSets are retained when their RES record or any resolved LOD payload
  physically belongs to the chosen subtree.
- Keep large category inventories compact with one category-matching result per
  loose or virtual asset folder. Search is applied before choosing that folder's
  representative, so the displayed row itself always matches the query.
- Right-click a result to enter a folder-only view containing every file from
  the selected category in that loose or virtual folder. Unrelated folders are
  hidden until **Back to all results** is clicked.
- Open Frostbite virtual locations such as `forttarsis.sb::exo` directly from
  the path box. The app resolves the real parent installation, then limits every
  category to that archive and internal prefix. A result's context menu can open
  its exact virtual folder, and **Back to all results** exits the virtual scope.
- Show ordinary PNG/JPEG/BMP/GIF/WebP/TGA/DDS and similar image assets directly in
  the embedded 2D viewport, scaled to fit the preview pane.
- Recognize common `.ifp`, `.anm`, `.xanim`, and `.ycd` animation files, plus
  filename/path hints for opaque animation and skeleton formats.
- Classify weapon, map, and prop archives without a per-game activation button.
- Decode the first texture in supported GTA IV `.wtd` dictionaries into the
  embedded 2D viewer (DXT1, DXT3/DXT5 colour inspection, A8R8G8B8, and L8).
- Index relevant GTA IV archive tables in the background so named WDR/WDD map
  and prop resources appear directly in their category filters, including map
  archives whose named drawable records do not carry the usual resource bit.
- Treat GTA IV WFT files as fragments/models, not skeletons. The old generic
  matrix fallback was removed because it could turn vehicle transforms into a
  convincing-looking but incorrect joint graph.
- Open Unity SerializedFiles and UnityFS/UnityRaw/UnityWeb AssetBundles through
  one capability adapter, including extensionless hashed bundles.
- Enumerate Unity Mesh, Texture2D, Sprite, AnimationClip, and real
  SkinnedMeshRenderer bone references as internal assets. This makes character,
  animation, texture/image, prop, map, skeleton, and weapon filters use object
  names from the container rather than the container filename.
- Preview Unity Mesh objects in 3D, Texture2D/Sprite objects in 2D, and verified
  SkinnedMeshRenderer bone hierarchies as connected joints inside the app.
- Probe at most 12 promising Unity containers per category instead of opening
  every bundle in an installation. Localization/string-table bundles are excluded
  from visual categories, and custom names still receive a small fallback sample.
- Run every Unity container inspection and mesh/texture/skeleton preview in a
  disposable child process. A corrupt bundle, native decoder failure, excessive
  allocation, or 30-second background timeout is reported as a notice instead of
  taking down the Tk/Python GUI.
- Serialize Unity category-indexing passes so rapidly switching between Animations,
  Textures, Props, and other filters cannot launch several heavy readers at once.
- Avoid copying Unity's complete object table and avoid automatically prioritizing
  unhinted multi-gigabyte monolithic files, reducing peak memory pressure.
- Open a selected raw Unity container on demand and preview its first compatible
  contained mesh, image, or verified skeleton. Containers containing only scripts,
  localization, or metadata now say so explicitly.
- Ignore Unity `.resS` companion streams as standalone assets; their bytes are
  resolved by the owning `.assets` file or bundle.
- Exclude GTA IV `.tune` object configuration files from Skeletons; files such
  as parking meters and glasses were false positives, not rigs.
- Finish the initial filesystem scan before archive indexing. Relevant GTA IV
  archives are indexed lazily in a background thread when Map parts, Props,
  Skeletons, or Weapons & carryables is selected, with per-archive progress.
- Keep archive rows stable after preview and cache their verified model in memory.
- Hide low-confidence meshes such as radar/plant resources unless requested.
- Route Frostbite `.toc`, `.sb`, `.cat`, and `.cas` containers to a distinct
  format capability, never to the Rockstar RPF/IMG reader.
- Parse Anthem-generation Frostbite `layout.toc`, TOC bundle indexes, typed SB
  resource tables, and their CAS locations. Real `MeshSet` RES records appear as
  ordinary meshes when the complete installation folder is scanned.
- Inventory animation-related Frostbite EBX/RES records under the Animations
  category without misclassifying MeshSet geometry merely because its virtual
  path begins with `animation/`. These records are marked indexed; schema,
  skeleton binding, and clip playback decoding remain future work.
- Separate named Frostbite skeleton records such as `exh_master_skeleton.ebx`
  from animation candidates even though the game stores them below an
  `animation/` virtual path. They appear in **Skeletons** as **rig indexed**.
- Decode a selected indexed Frostbite skeleton or animation EBX/RES record from
  its CAS payload with **Extract selected…**. Extraction runs in a disposable
  worker and writes both the decoded record and a JSON location descriptor,
  providing verified inputs for the next skeleton-schema and playback work.
- Parse schema-independent EBX file/import GUIDs. When an Anthem master-skeleton
  entry is a small alias, **Extract selected…** now searches its owning bundle
  first and then the installation metadata for the linked EBX, extracting that
  real payload and its JSON descriptor alongside the alias. Animation extraction
  remains direct and does not perform this dependency scan.
- Decode Anthem `SkeletonAsset` bone names, parent indices, and validated
  model-space bind transforms from the linked master-skeleton payload. Selecting
  a skeleton now previews its connected 183-bone Javelin hierarchy directly.
- Match a Javelin MeshSet family such as `exf`, `exh`, or `exm` to its indexed master
  skeleton automatically. The viewport's **Bones** toggle draws the bind-pose
  hierarchy over the mesh in the same coordinate space, while the Rig Inspector
  confirms whether the skeleton and section palettes are attached.
- Preserve every decoded vertex's four SkeletonAsset bone IDs and normalized
  skin weights instead of discarding them after rig inspection. This is the
  deformation input required by future animation playback.
- Lock one fully decoded mesh, skeleton, palette, and skin-weight set with the
  square **Use for animation** checkbox. The active character remains loaded
  while switching to **Animations**, and a visible status strip identifies it.
- Keep Preview, **Use for animation**, Rig Inspector, extraction, report, and
  notice controls pinned on-screen even when a long decoding status is shown.
- Compare animation-path Javelin family hints with the active character, load a
  selected animation EBX/RES payload from CAS, and report its verified EBX
  imports/arrays without falsely claiming that a configuration record is a
  playable clip. Playback controls remain disabled until compressed keyframes
  are decoded.
- Decode every embedded Anthem `EclipseAnimationAsset` clip from a selected
  AntState RES, including 8/16-bit key times, quantized float/vector curves,
  dynamic unit-quaternion curves, exact clip names, and `ChannelToDofAsset`
  identifier lists. The clip selector shows real clips instead of heuristic
  byte-pattern groups. Selecting another clip recomputes its own mapping.
- Refuse unsafe animation playback while the referenced Rigamate bank is absent.
  Anthem's DOF IDs are bank-local identifiers, not SkeletonAsset bone indices;
  sequential assignment caused the scrambled poses. Dynamic quaternion words
  are no longer misread as Euler angles.
- Decode real `BankPointerAsset` records from AntState payloads, distinguish a
  locally resolved ActionStation pointer from an external Rigamate pointer, and
  display the exact eight-byte `SubjectBankAsset` key. When an external
  Rigamate pointer is selected, search decoded RES records across the Anthem
  installation (owning and Javelin-family bundles first), report the resolved
  virtual resource or a bounded not-found result, and enable experimental
  playback only when the bank IDs and named skeleton joints match.
- Keep the successfully resolved Rigamate payload available in memory and
  enable **Extract Rigamate bank…** beside the animation diagnostics. The
  button writes the exact decoded `.res` plus its archive-location JSON without
  modifying Anthem, providing the concrete bank sample needed to reverse its
  DOF-to-bone table rather than guessing from filenames or skeleton order.
- Resolve a selected Frostbite MeshSet and its declared geometry chunk by typed
  record id, verify both compressed SHA-1 values, decode their bounded CAS block
  streams with the game's installed Oodle runtime, and render the lowest-detail
  available LOD in the embedded viewport.
- Show a low-to-high Detail slider after a Frostbite MeshSet preview. It switches
  between the real LOD geometry stored by the game (normally LOD5 toward LOD0),
  rather than inventing smoother polygons, and decodes the chosen LOD in the
  crash-contained worker while leaving the previous preview visible on failure.
- Resolve declared Frostbite LOD chunks across matching base `Data` and `Patch`
  bundle layers, with patch precedence. This exposes LOD2, LOD1 and the true
  highest-detail LOD0 when a patched MeshSet references geometry retained in
  the base installation instead of its immediate `.sb` file.
- Catalogue chunk GUIDs across the complete Frostbite installation and keep a
  compact indexed cache. A MeshSet can therefore resolve LOD0/LOD1/LOD2 geometry
  stored in a differently named shared streaming or customization bundle; each
  preview queries only the handful of GUIDs declared by that MeshSet.
- Inspect every decoded Frostbite MeshSet header during the isolated inventory
  pass and mark which declared LOD GUIDs resolve to installed CAS payloads.
  **LOD0 available only**, beside the category selector, filters Characters /
  meshes to genuine LOD0 matches instead of treating every declaration as proof
  that the high-detail data is present. Confirmed rows show `LOD0 ready`.
- Keep Detail, Reset view, and Wireframe visible when an internal asset has a
  very long name. MeshSets exposing only one usable geometry chunk say `only`
  instead of displaying an unusable slider or misleading higher-detail prompt.
- Render every stored triangle in the settled high-detail viewport. Camera
  movement retains a bounded quick preview, then a background bitmap pass
  replaces it with the complete model instead of dropping alternating faces
  after an arbitrary Tk polygon budget.
- Inspect Frostbite MeshSet vertex declarations for paired skin-index/weight
  streams. Verified evidence changes the selected row from `Rig —` to
  `Rig likely` and is explicitly distinguished from a decoded skeleton or
  playable animation.
- Decode the selected Anthem MeshSet's four bone-index and four skin-weight
  bytes per vertex. Confirmed rows change to `Rig skinned`, and **Inspect rig…**
  reports every skinned section, weighted-vertex coverage, referenced palette
  slots, stream numbers, and raw weight-sum validation.
- Decode each Anthem MeshSet section's bone palette and map local vertex-skin
  slots to skeleton bone IDs. The Rig Inspector reports both the number of
  referenced slots and resolved numeric skeleton IDs. When the matching master
  skeleton is indexed, those IDs are validated against its named hierarchy and
  the bind pose can be displayed over the mesh.
- Run Frostbite installation indexing and selected-mesh decoding in disposable
  child processes with timeouts. A malformed table, native decoder failure, or
  excessive scan is reported without taking down the GUI.
- Mark MeshSet wrappers or records without an available geometry chunk as
  `no geometry` after inspection and show the reason in the viewport instead of
  repeatedly opening an error dialog.
- Keep the GTA IV Skeletons filter responsive. WFT fragments are no longer
  treated as rigs or used to trigger a CPU-heavy pass over every pedestrian and
  vehicle archive.
- Deduplicate repeated Frostbite MeshSet references by content hash and name,
  and group the resulting typed assets with the same neutral category logic as
  loose, Unity, and RAGE assets.
- Keep raw Unity and Frostbite files under Containers / archives. A `.toc`, `.sb`,
  or AssetBundle is no longer mislabeled as a rendered map or prop merely because
  of its path; only typed or decoded children enter semantic categories.
- Explain an empty GTA IV Skeletons view instead of leaving stale mesh geometry in
  the viewport. No vehicle transform cloud is presented as a character skeleton.
- Validate and decode bounded Frostbite CAS records. Oodle-compressed records use
  the 64-bit Oodle runtime already installed with the user-selected game; the app
  does not bundle, download, or redistribute that proprietary DLL.
- Inspect decoded Anthem-generation MeshSet headers without guessing, including
  bounding boxes, mesh type, LOD/section counts, and the real CAS chunk GUIDs that
  hold vertex and index buffers.
- Decode and render loose Anthem-generation `.meshset` resources when their
  decoded external LOD chunk is beside them. Vertex streams, renderable section
  categories, 16-bit triangle indices, Float3/Float4 and Half3/Half4 positions
  are read from the resource declarations rather than guessed. Depth and shadow
  duplicates are excluded from the viewport.

## Important limitation

Commercial games commonly store assets inside proprietary containers. Unity
`.assets` and AssetBundles have a typed reader. Anthem-generation Frostbite
MeshSets now have a TOC/SB/CAS reader and geometry interpreter; textures,
animation clips and unsupported rig variants from Frostbite, plus Unreal and
other containers, still need matching format-generation adapters. This project deliberately
does not bypass encryption, DRM, access controls, or ownership checks.

For a Frostbite installation such as Anthem, `.toc` and `.sb` files are metadata
and `.cas` files hold most payload bytes. The explorer reports these relationships
and follows typed MeshSet records on demand without unpacking the complete CAS
collection. See `FROSTBITE_DECODING.md` for the concrete CAS/decompression/MeshSet
pipeline.

## Quick start on Windows

1. Install Python 3.11 or newer.
2. Double-click `run_windows.bat`.
3. Choose the game installation folder and click **Scan**.
4. Select a result and click **Preview** (or double-click the row).

No manual format-package setup is required. Optional archive readers are loaded on
demand when indexing or Preview first needs them.

### Unity games (including Pixel Gun 3D PC Edition)

After the filesystem scan finishes, a bounded set of category-relevant Unity
containers is indexed in a background pass. The list is rebuilt from the typed objects found inside them, so a
whole `sharedassets11.assets` file is no longer mislabeled as a weapon and `.resS`
streams no longer appear as selectable assets. Hashed extensionless bundles are
recognized from UnityFS/UnityRaw/UnityWeb signatures rather than their filenames.
Use Containers / archives to inspect a particular bundle directly; Preview opens
the bundle and chooses a real contained object when a supported one exists.

The Characters / meshes view ranks meshes referenced by a SkinnedMeshRenderer above
ordinary environment meshes. Textures and Sprites use the embedded 2D viewport;
Meshes use the embedded 3D viewport; Skeletons are created only from an actual Unity
renderer bone list and Transform parent links. Animation clips are inventoried in
v0.13; timeline playback is intentionally deferred.

### GTA IV

GTA IV stores pedestrian drawables in two distinct container families: encrypted
RPF2/RPF3 archives and flat IMG3 component archives. The extension alone does not
identify the format. Version 0.8 reads the file signature and dispatches to the
correct parser automatically, including the AES-obfuscated IMG3 headers used by the
base game and both Episodes. IMG3 preserves `.wdr`, `.wft`, and `.wtd` names;
RPF3 entries may have only a hash, so their resource flags are used instead.

There is no game-specific activation step. Select an archive and click **Preview**.
The capability registry recognizes RPF3 and loads its cryptographic reader when first
needed. Preview inspects only the selected archive, enumerates hashed resource records,
then probes their binary structure as drawable dictionaries and standalone drawables
until one genuinely decodes. It does not depend on a filename list. The reader uses
the matching key from your own `GTAIV.exe`; the explorer never writes to the archive.

After discovery, **Extract selected…** can copy a matching drawable/texture group to a
separate folder. Extraction does not modify the GTA IV installation.

The app has its own dependency-free 3D viewport. WDD/WDR decoding remains experimental:
supported GTA IV vertex layouts are rendered directly, while unsupported structures
produce a short “not a character model” notice instead of raw binary-parser errors.

The default list contains likely characters and character archives. Enable **Show
low-confidence assets** to inspect every loose mesh candidate. This is a generic
confidence filter, not a game-specific activation switch.

### Asset categories

The category selector below the game path is an inventory filter. Characters / meshes
keeps the ranked view; the other categories show matching scanned files, including
low-confidence files, so they are useful for exploring a new game. Classification is
based on detected asset kind and neutral filename/path terms, not a list of supported
games. Generic engine containers have their own Containers / archives category;
semantic categories are populated from decoded or typed child records. Proprietary
texture dictionaries such as GTA IV `.wtd` still require a format-specific decoder,
while standard image files use the in-app 2D viewer immediately. Unsupported
WTD pixel formats report the format instead of crashing. Unity object categories are
derived from their serialized type and internal name rather than a per-game list.

## Command line

```powershell
python run.py scan "D:\Games\Some Game" --json scan-report.json
python run.py first "D:\Games\Some Game"
```

Useful options:

```text
--max-files N        stop after N files (default: 500000)
--include-hidden     scan hidden folders too
```

The legacy `view` command remains available for workflows that explicitly want
Blender, but the desktop GUI's **Preview** action is fully in-app.

## Adapter design

The generic scanner is intentionally separate from extraction. A future adapter can
turn proprietary records into `AssetRecord` objects, after which the grouping, ranking,
reporting, GUI and neutral mesh viewer work unchanged. See `game_asset_explorer/engines.py`.
Adapters are registered by capabilities such as `rpf3-read`, not by adding one button
per game. Readers can identify entries from extensions, archive metadata, magic bytes,
or structural probes. See `game_asset_explorer/adapters.py`.

## Safety and legal scope

Use this only with files you are legally entitled to inspect. The scanner is read-only.
An adapter may decode an archive table using a key present in your own executable, but
the program does not patch executables, contact game services, bypass ownership checks,
or redistribute game assets. Exported models remain subject to the game's licence and
copyright.
