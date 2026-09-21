"""CPU linear-blend skinning for decoded Frostbite MeshSet sections."""
from __future__ import annotations

from .geometry import MeshData, SkeletonData
from .frostbite_animation_playback import Quaternion, quaternion_multiply


def skin_meshes(
    meshes: list[MeshData], bind: SkeletonData, pose: SkeletonData,
    bind_world: tuple[Quaternion, ...], pose_world: tuple[Quaternion, ...],
) -> list[MeshData]:
    """Transform weighted vertices in model space through each bone's bind pose."""
    count = len(bind.joints)
    if count != len(pose.joints) or count != len(bind_world) or count != len(pose_world):
        raise ValueError("The posed skeleton must have the same bones as the bind skeleton.")
    transforms = []
    for j in range(count):
        b = bind_world[j]
        p = pose_world[j]
        qx, qy, qz, qw = quaternion_multiply(p, (-b[0], -b[1], -b[2], b[3]))
        # Precompute the bone's rotation matrix once, outside the vertex loop.
        rotation = (
            1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw),
            2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw),
            2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy),
        )
        transforms.append((rotation, bind.joints[j][2:5], pose.joints[j][2:5]))
    posed = []
    for mesh in meshes:
        if mesh.skin_bones is None or mesh.skin_weights is None or \
                len(mesh.skin_bones) != len(mesh.vertices) or len(mesh.skin_weights) != len(mesh.vertices):
            posed.append(mesh)
            continue
        vertices = []
        for vertex, bones, weights in zip(mesh.vertices, mesh.skin_bones, mesh.skin_weights):
            x, y, z = vertex
            result = [0.0, 0.0, 0.0]
            total = 0.0
            for bone, weight in zip(bones, weights):
                if weight <= 0 or bone < 0 or bone >= count:
                    continue
                rot, origin, destination = transforms[bone]
                dx, dy, dz = x - origin[0], y - origin[1], z - origin[2]
                result[0] += weight * (rot[0] * dx + rot[1] * dy + rot[2] * dz + destination[0])
                result[1] += weight * (rot[3] * dx + rot[4] * dy + rot[5] * dz + destination[1])
                result[2] += weight * (rot[6] * dx + rot[7] * dy + rot[8] * dz + destination[2])
                total += weight
            vertices.append(tuple(v / total for v in result) if total else vertex)
        posed.append(MeshData(mesh.name, vertices, mesh.faces, mesh.skin_bones, mesh.skin_weights))
    return posed
