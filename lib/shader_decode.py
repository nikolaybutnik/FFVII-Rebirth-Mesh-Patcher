"""
shader_decode.py -- the REAL decode, transcribed from the game's vertex shader.

Recovered from a RenderDoc capture of FF7 Rebirth (SM6.6 DXIL vertex shader,
draw 3097). The tangent stream is bound as R10G10B10A2_UNORM, so the hardware
splits the 32 bits into four values before the shader sees them:

    x = bits  0..9  / 1023      U
    y = bits 10..19 / 1023      V
    z = bits 20..29 / 1023      angle
    w = bits 30..31 / 3         two flag bits

NORMAL  (matches what was derived from data)
    px = U - V
    py = U + V - 1
    pz = 1 - |px| - |py| , negated unless bit30 is set
    N  = normalize(px, py, pz)

REFERENCE BASIS  -- Frisvad / Duff branchless orthonormal basis
    s = (Nz >= 0) ? -1 : +1
    a = 1 / (Nz - s)
    E1 = ( 1 + s*Nx*Nx*a ,  s*Nx*Ny*a ,  s*Nx )
    E2 = (     Nx*Ny*a   ,  s + Ny*Ny*a ,   Ny )

ANGLE  -- and this is the part that could not be guessed from the data.
It is NOT a linear angle. It is the unit circle parameterised on the DIAMOND
|x| + |y| = 1:

    n   = angle & 255          (low 8 bits)
    t   = n / 255
    cx  = (angle & 256) ? t : -t
    cy  = (angle & 512) ? (1 - t) : -(1 - t)
    (cos, sin) = normalize(cx, cy)

Walking t from 0 to 1 traverses one edge of the diamond, and bits 8 and 9 pick
which of the four edges. Because the diamond is not a circle, equal steps in t
are NOT equal steps in angle -- which is exactly why every linear-angle model
fitted here plateaued, and why the apparent "reference direction" seemed to
scatter by 5-10 degrees for a fixed normal.

    T = cos * E1 + sin * E2
    B = cross(T, N) * (bit31 ? +1 : -1)
"""

import numpy as np


def decode(words):
    """Decode packed uint32 tangent words -> (N, T, B). Exact."""
    w = np.asarray(words).astype(np.uint32).astype(np.int64)

    U = (w & 1023) / 1023.0
    V = ((w >> 10) & 1023) / 1023.0
    ang = (w >> 20) & 1023
    flags = (w >> 30) & 3

    bit30 = (flags & 1) != 0
    bit31 = (flags & 2) != 0

    # ---- normal ----
    px = U - V
    py = U + V - 1.0
    pz = 1.0 - np.abs(px) - np.abs(py)
    pz = np.where(bit30, pz, -pz)

    P = np.stack([px, py, pz], axis=1)
    N = P / np.maximum(np.linalg.norm(P, axis=1, keepdims=True), 1e-20)
    Nx, Ny, Nz = N[:, 0], N[:, 1], N[:, 2]

    # ---- Frisvad/Duff basis, exactly as the shader builds it ----
    s = np.where(Nz >= 0.0, -1.0, 1.0)
    a = 1.0 / (Nz - s)
    e1 = np.stack([1.0 + s * Nx * Nx * a, s * (Nx * Ny * a), s * Nx], axis=1)
    e2 = np.stack([Nx * Ny * a, s + Ny * Ny * a, Ny], axis=1)

    # ---- diamond-parameterised angle ----
    n8 = ang & 255
    t = n8 / 255.0
    cx = np.where((ang & 256) != 0, t, -t)
    cy = np.where((ang & 512) != 0, 1.0 - t, -(1.0 - t))
    inv = 1.0 / np.maximum(np.sqrt(cx * cx + cy * cy), 1e-20)
    c = cx * inv
    sn = cy * inv

    T = e1 * c[:, None] + e2 * sn[:, None]
    T = T / np.maximum(np.linalg.norm(T, axis=1, keepdims=True), 1e-20)

    B = np.cross(T, N) * np.where(bit31, 1.0, -1.0)[:, None]
    return N, T, B


def encode(N, T, handed_positive=None):
    """
    Inverse: tangent frames -> packed uint32 words.

    `handed_positive` is a boolean array; if omitted it is derived from the
    sign of dot(cross(T, N), B) which the caller must otherwise supply.
    """
    N = np.asarray(N, dtype=np.float64)
    T = np.asarray(T, dtype=np.float64)

    # ---- normal -> U, V, bit30 ----
    L1 = np.abs(N).sum(axis=1, keepdims=True)
    p = N / np.maximum(L1, 1e-20)
    px, py, pz = p[:, 0], p[:, 1], p[:, 2]
    # px = U - V, py = U + V - 1  =>  U = (px + py + 1)/2, V = (py - px + 1)/2
    U = (px + py + 1.0) * 0.5
    V = (py - px + 1.0) * 0.5
    qu = np.clip(np.rint(U * 1023.0), 0, 1023).astype(np.int64)
    qv = np.clip(np.rint(V * 1023.0), 0, 1023).astype(np.int64)
    bit30 = (pz >= 0)

    # ---- rebuild the basis from the QUANTISED normal, as the GPU will ----
    Uq = qu / 1023.0
    Vq = qv / 1023.0
    ax = Uq - Vq
    ay = Uq + Vq - 1.0
    az = 1.0 - np.abs(ax) - np.abs(ay)
    az = np.where(bit30, az, -az)
    P = np.stack([ax, ay, az], axis=1)
    Nq = P / np.maximum(np.linalg.norm(P, axis=1, keepdims=True), 1e-20)
    Nx, Ny, Nz = Nq[:, 0], Nq[:, 1], Nq[:, 2]

    s = np.where(Nz >= 0.0, -1.0, 1.0)
    a = 1.0 / (Nz - s)
    e1 = np.stack([1.0 + s * Nx * Nx * a, s * (Nx * Ny * a), s * Nx], axis=1)
    e2 = np.stack([Nx * Ny * a, s + Ny * Ny * a, Ny], axis=1)

    # ---- project T onto the basis, then diamond-encode the direction ----
    c = np.sum(T * e1, axis=1)
    sn = np.sum(T * e2, axis=1)
    m = np.maximum(np.abs(c) + np.abs(sn), 1e-20)
    dx = c / m                       # now |dx| + |dy| = 1
    dy = sn / m

    # t is |dx| along the edge; the two sign bits pick the edge
    t = np.abs(dx)
    n8 = np.clip(np.rint(t * 255.0), 0, 255).astype(np.int64)
    b8 = (dx >= 0).astype(np.int64)
    b9 = (dy >= 0).astype(np.int64)
    ang = n8 | (b8 << 8) | (b9 << 9)

    w = (qu | (qv << 10) | (ang << 20) | (bit30.astype(np.int64) << 30))
    if handed_positive is not None:
        w = w | (np.asarray(handed_positive).astype(np.int64) << 31)
    return w.astype(np.uint32)


if __name__ == "__main__":
    # Self-test: generate random tangent frames, encode them, decode them back,
    # and check we land where we started. This exercises the same code the
    # patcher uses, with no external data required.
    rng = np.random.default_rng(0)
    n = 200000

    N = rng.normal(size=(n, 3))
    N /= np.linalg.norm(N, axis=1, keepdims=True)

    # a random tangent perpendicular to each normal
    R = rng.normal(size=(n, 3))
    T = R - N * np.sum(R * N, axis=1, keepdims=True)
    T /= np.linalg.norm(T, axis=1, keepdims=True)

    hand = rng.random(n) < 0.6

    words = encode(N, T, hand)
    N2, T2, B2 = decode(words)

    dn = np.degrees(np.arccos(np.clip(np.sum(N2 * N, axis=1), -1, 1)))
    dt = np.degrees(np.arccos(np.clip(np.sum(T2 * T, axis=1), -1, 1)))
    perp = np.abs(np.sum(T2 * N2, axis=1))

    print("round-trip over %d random frames" % n)
    print("  normal  : median %.4f deg   99th %.4f deg   max %.4f deg"
          % (np.median(dn), np.percentile(dn, 99), dn.max()))
    print("  tangent : median %.4f deg   99th %.4f deg   max %.4f deg"
          % (np.median(dt), np.percentile(dt, 99), dt.max()))
    print("  T perpendicular to N: max |dot| = %.2e" % perp.max())
    print("  handedness preserved : %.2f%%"
          % (np.mean(((words >> np.uint32(31)) & np.uint32(1)).astype(bool)
                     == hand) * 100))

    ok = np.percentile(dn, 99) < 0.5 and np.percentile(dt, 99) < 1.0
    print()
    print("  SELF-TEST %s" % ("PASSED" if ok else "FAILED"))
