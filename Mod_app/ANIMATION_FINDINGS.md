# Anthem animation findings

## Corrected in 0.27.26

The Interceptor `EXF_NOV_Walk_Start_Turn180Right` dump proved that the long
lines were not corrupt body rotations. `AITrajectory`, `GroundPlane`,
`ClimbPlane`, `Camera*`, and `Connect*` are authored gameplay/controller
anchors. Some deliberately counter-transform the trajectory and therefore
remain near world origin while the render skeleton moves. The viewer now
hides those non-deforming joints and any bone line attached to them, but keeps
their channels decoded and evaluated for future root-motion, contact, camera,
and gameplay-controller reconstruction.

## Corrected in 0.27.25

Some valid locomotion clips carry absolute gameplay-space placement rather
than a viewer-local starting point. In the Interceptor pistol backward-strafe
sample, `AITrajectory.t` is constantly Z=5.855 and `GroundPlane.t` is X=-6.030.
Those values explain the two long skeleton lines in the user's capture; the
body animation and named bone mapping are otherwise correct. Preview now
subtracts the trajectory root's earliest sample (preserving later root-motion
deltas). Version 0.27.26 supersedes the temporary plane hold: the channels are
now retained and only their debug-overlay joints are hidden.

## Corrected in 0.27.24

Interceptor animation records carry an authored `PrimaryRig` key independent
of their smaller Rigamate curve-bank pointer. That key resolves to the global
animation bank, which contains separate named EXF, EXM, and EXH RigAssets.
Selecting the EXF RigAsset by its exact eight-byte key and joining its 28 DOF
sets maps `EXF_NOV_Glide_Down_Boost_Start` to 70 bones in the decoded 202-bone
Interceptor skeleton; 17 rotation helpers and their translation companions
are intentionally skipped because they are absent from the render skeleton.

The old experimental list-order fallback mapped channel zero to Hips and later
channels to weapon sockets, producing the exploded pose in the user's debug
capture. Non-Lancer families no longer use that unsafe fallback. A compact
worker-side extraction returns only the selected rig mapping (about 114 KB)
instead of transferring the roughly 58 MB global bank to the UI.

## Corrected again in 0.28.5

Version 0.28.4 incorrectly treated the low bits of the first two 21-bit fields
as omitted-axis selectors. A near-identity real constant splits into signed
fields `(3, 1, 1)`; consuming two of those precision bits changes the omitted
axis and creates the folded Interceptor poses. Constants are restored to three
signed 21-bit X/Y/Z fields with W reconstructed from bit 63's sign. The small
set of words outside the unit bound remains invalid and falls back to bind
instead of being normalized or reinterpreted.

## Confirmed 0.27.18 interpretation

Inline one-key rotations use signed 21-bit X/Y/Z fields over
`[-1/sqrt(2), +1/sqrt(2)]`; bit 63 supplies W's sign. Moving keys retain their
separate 48-bit smallest-three representation.

The idle's exact 180-degree `LeftToeRear`/`RightToeRear` constants remain real
decoded evidence. They are now held at bind pose as a narrow named-helper
playback safeguard, leaving all other constants untouched.

Clips without an authored ChannelToDof reference can now be tried with an
explicitly labelled inferred bank map or experimental primary-rig-order map.
Exact keyed maps remain preferred. Clip playback can also be rendered offline
to a 960×720 Motion JPEG AVI at 15 fps.

## Implemented in 0.27.14

Some action-station resources embed curves but store the referenced
`ChannelToDofAsset` in the external Rigamate bank. The installed
`EXM_EXP_ExamineObject_Low` crouch clip has 129 channels and trailer key
`4d3594ffcd5f4b79`; that exact bank object supplies all 129 DOF identifiers.
Resolving it enables 72 named rotations and four render-skeleton translations.

`Hips.t`, the two hand-prop translations, and `HeadCamera.t` are now evaluated
as absolute local offsets. Rotation and translation sampling share the clip's
global tick range, and controller FPS/TimeScale determine playback duration.
The crouch clip produces a coherent crouching pose in the diagnostic renderer.

## Implemented in 0.27.13

Mapped Eclipse quaternion curves are absolute joint-local rotations. The old
playback subtracted each Rigamate default and then multiplied the result by the
EBX bind rotation. On the exact supplied `Friendly_Idle`, `Twitch5`, and
`Twitch9` clips, that rule collapses the skeleton into a knot in all sampled
phases. Applying the stored curve directly as the animated joint's local
rotation produces a coherent upright skeleton across the same phases. The
viewer, skinning path, and pose-debug export now all use that composition;
unanimated joints continue to use their EBX bind rotations.

## Superseded 0.27.16 direct-XYZ hypothesis

The direct-X/Y/Z interpretation was a local visual hypothesis based on the two
rear-toe helpers. The later full-character idle capture disproved it: a real
pelvis constant requires the selector layout to reconstruct its authored
rotation. Version 0.27.18 restores smallest-three and handles the two helpers
at playback policy level instead.

The decoder retains the original eight bytes for diagnostics and exposes a
single key at time zero, so the existing interpolation and Rigamate mapping
paths apply it for the whole clip. Constant rotations no longer disappear from
the pose and no longer fall back to the mesh bind rotation.

## Validation

- 18 constants decoded in the supplied Crouch resource.
- 735 constants decoded across the six supplied Lancer preview clips.
- 87 constants decoded across three standing-guard clips extracted from the
  installed game.
- Every decoded value above reconstructs to a unit quaternion.
- One real near-identity EXM constant differs from its authored Rigamate
  default by about 0.0002 degrees, consistent with quantization error.
- In the three resolved standing-guard clips, 20, 22, and 22 mapped constant
  bone rotations respectively now reach playback alongside moving curves.

## What Frosty, Maya, and the executable tell us

The attached Frosty build contains the Anthem type SDK and generic Frostbite
asset infrastructure, but no animation editor/preview plugin. Its profile is
useful for reflected type names and field layouts; it does not provide the ANT
runtime curve evaluator needed here.

Maya explains the authored skeleton, controllers, constraints, and export
workflow, but the shipped RES data has already passed through EA's ANT/Eclipse
compiler. The blocker was the runtime compression and Rigamate mapping layer,
not a Maya file structure, so reverse-engineering Maya would not recover this
codec.

The executable recon toolkit correctly found that type strings and obvious
floating-point constants do not lead directly to the evaluator. Anthem's large
custom sections also contain data that is not safely treated as linearly
disassembled code. Cross-file invariants from the animation RES, Rigamate bank,
and EBX skeleton provide stronger evidence for this decoder than a guessed EXE
match.

## Remaining work before claiming game-accurate animation

1. Resolve auxiliary IK/procedural vector channels that do not exist in the
   182-joint render skeleton.
2. Decode controller Modes and sequence blend scheduling.
3. Determine whether Twitch clips are additive layers and how their blend-curve
   scalar is interpreted.

Rotation and named translation playback are materially more complete in
0.27.14, but the remaining procedural and sequence rules are why playback is
not yet claimed as game-identical.
