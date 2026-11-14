// Works out the folder a password-protected export keeps its photos in: PBKDF2 with SHA-256 over
// the password and the export's salt, as api/exports.py does.  Written out here, not left to the
// browser's own crypto, since browsers only offer that over HTTPS, and S3 websites are plain HTTP.
window.exportFolder = (function () {
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ]);
  const START = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
  const w = new Uint32Array(64);

  // one 64-byte block, as 16 words, into a hash's 8 words of state
  function compress(state, block) {
    for (let i = 0; i < 16; i++) {
      w[i] = block[i];
    }
    for (let i = 16; i < 64; i++) {
      const a = w[i - 15], b = w[i - 2];
      const s0 = ((a >>> 7) | (a << 25)) ^ ((a >>> 18) | (a << 14)) ^ (a >>> 3);
      const s1 = ((b >>> 17) | (b << 15)) ^ ((b >>> 19) | (b << 13)) ^ (b >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    let [a, b, c, d, e, f, g, h] = state;
    for (let i = 0; i < 64; i++) {
      const t1 = (h + (((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7))) + ((e & f) ^ (~e & g)) + K[i] + w[i]) | 0;
      const t2 = ((((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10))) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
      h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
    }
    return [state[0] + a | 0, state[1] + b | 0, state[2] + c | 0, state[3] + d | 0, state[4] + e | 0, state[5] + f | 0, state[6] + g | 0, state[7] + h | 0];
  }

  function words(bytes) {
    const out = new Array(bytes.length / 4);
    for (let i = 0; i < out.length; i++) {
      out[i] = (bytes[4 * i] << 24) | (bytes[4 * i + 1] << 16) | (bytes[4 * i + 2] << 8) | bytes[4 * i + 3];
    }
    return out;
  }

  // SHA-256 of any bytes, after a state that has already taken in `before` bytes
  function sha256(bytes, state = START, before = 0) {
    const length = bytes.length + before;
    const padded = new Uint8Array(Math.ceil((bytes.length + 9) / 64) * 64);
    padded.set(bytes);
    padded[bytes.length] = 0x80;
    const bits = length * 8;
    const view = new DataView(padded.buffer);
    view.setUint32(padded.length - 8, Math.floor(bits / 0x100000000));
    view.setUint32(padded.length - 4, bits >>> 0);
    const all = words(padded);
    for (let i = 0; i < all.length; i += 16) {
      state = compress(state, all.slice(i, i + 16));
    }
    return state;
  }

  function toBytes(state) {
    const out = new Uint8Array(32);
    state.forEach((word, i) => new DataView(out.buffer).setUint32(4 * i, word >>> 0));
    return out;
  }

  // a 32-byte digest followed by SHA-256's padding for a message 64 + 32 bytes long, as a block
  function digestBlock(state) {
    return [...state, 0x80000000, 0, 0, 0, 0, 0, 0, (64 + 32) * 8];
  }

  function pbkdf2(password, salt, iterations) {
    let key = new TextEncoder().encode(password);
    if (key.length > 64) {
      key = toBytes(sha256(key));
    }
    const pad = byte => {
      const block = new Uint8Array(64).fill(byte);
      key.forEach((value, i) => { block[i] ^= value; });
      return compress(START, words(block));
    };
    // the key's halves of HMAC, worked out once
    const inner = pad(0x36);
    const outer = pad(0x5c);
    const first = new Uint8Array(salt.length + 4);
    first.set(salt);
    first[salt.length + 3] = 1;
    let u = compress(outer, digestBlock(sha256(first, inner, 64)));
    const result = u.slice();
    for (let i = 1; i < iterations; i++) {
      u = compress(outer, digestBlock(compress(inner, digestBlock(u))));
      for (let j = 0; j < 8; j++) {
        result[j] ^= u[j];
      }
    }
    return toBytes(result);
  }

  return function (password, saltHex, iterations) {
    const salt = new Uint8Array(saltHex.match(/../g).map(pair => parseInt(pair, 16)));
    return [...pbkdf2(password, salt, iterations)].map(byte => byte.toString(16).padStart(2, '0')).join('').slice(0, 32);
  };
})();
