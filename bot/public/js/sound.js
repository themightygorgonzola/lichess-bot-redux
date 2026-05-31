/**
 * sound.js — Web Audio API chess sound effects.
 *
 * Sound.move()     — piece placed
 * Sound.capture()  — piece captured
 * Sound.toggle()   — mute / unmute
 * Sound.enabled    — current state
 *
 * ── Layer recipe system ─────────────────────────────────────────────────
 * Each sound is an array of layer descriptors. Three layer types:
 *
 *   Oscillator:
 *     { type:'osc', wave, freq, endFreq?, dur, vol, delay? }
 *
 *   Filtered noise:
 *     { type:'noise', dur, vol, decay?, filter?:{kind,freq,Q?}, delay? }
 *     filter.kind = 'lowpass'|'bandpass'|'highpass'  (default: no filter)
 *     Raw noise → harsh hiss.  Lowpass ≈ woody thud.  Bandpass ≈ clack/crack.
 *
 *   FM synthesis  (one oscillator modulating another's frequency):
 *     { type:'fm', carrier, ratio, depth, dur, vol, wave?, delay? }
 *     Modulator freq  = carrier × ratio
 *     Modulation depth decays from `depth` Hz → 0 over `dur` seconds.
 *     This is what gives impacts their non-electronic "bonk" / "thwack" character.
 *
 * ── Tuning quick-reference ──────────────────────────────────────────────
 *   noise filter freq 150–300 Hz  → deep wooden thud
 *   noise filter freq 400–800 Hz  → bright clack / crack
 *   osc freq 180–300 Hz           → heavy piece resonance
 *   osc freq 320–450 Hz           → lighter piece resonance
 *   fm ratio 0.5                  → sub-octave growl under the carrier
 *   fm ratio 1.0                  → bell-like strike (same freq, adds complexity)
 *   fm ratio 2.0                  → bright metallic hit
 *   fm depth high (100–200 Hz)    → punchy, physical; depth low (20–50 Hz) → subtle colour
 *   jitter ±6–8%                  → stops identical rapid moves sounding robotic
 */
'use strict';

const Sound = (() => {
  let _ctx        = null;
  let _compressor = null;   // single master compressor — smooths peaks, removes harshness
  let _enabled    = localStorage.getItem('sound-enabled') !== 'false';

  function _getCtx() {
    if (!_ctx) _ctx = new (window.AudioContext || window.webkitAudioContext)();
    return _ctx;
  }

  /**
   * Lazily create a master DynamicsCompressor and return it as the destination
   * for all synthesis nodes.  Using a compressor instead of ctx.destination
   * directly prevents the hard clipping that makes transients sound harsh.
   */
  function _getDest(ctx) {
    if (!_compressor) {
      _compressor = ctx.createDynamicsCompressor();
      _compressor.threshold.value = -22;   // dB — starts compressing here
      _compressor.knee.value      =  10;   // dB — soft-knee width
      _compressor.ratio.value     =   3;   // input:output ratio above threshold
      _compressor.attack.value    = 0.002; // s  — fast enough to catch transients
      _compressor.release.value   = 0.12;  // s
      _compressor.connect(ctx.destination);
    }
    return _compressor;
  }

  function _canPlay() {
    return _enabled && _ctx && _ctx.state === 'running';
  }

  function _unlockAudio() {
    if (_ctx && _ctx.state === 'suspended') {
      _ctx.resume();
    } else if (!_ctx) {
      try { _ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (_) {}
    }
  }
  ['click', 'keydown', 'touchstart'].forEach(ev =>
    document.addEventListener(ev, _unlockAudio, { once: true, passive: true })
  );

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && _ctx && _ctx.state === 'suspended') _ctx.resume();
  });

  setInterval(() => {
    if (_ctx && _ctx.state === 'suspended' && !document.hidden) _ctx.resume();
  }, 20_000);

  // ── Synthesis primitives ────────────────────────────────────────────────

  function _jitter(freq, amount = 0.07) {
    return freq * (1 + (Math.random() * 2 - 1) * amount);
  }

  function _layer(ctx, dest, now, def) {
    const t0 = now + (def.delay || 0);

    if (def.type === 'noise') {
      const decayDur = def.decay || def.dur;
      const attack   = def.attack || 0;   // optional gain ramp-in — smooths the hard rectangular onset
      const bufLen   = Math.ceil(ctx.sampleRate * def.dur);
      const buffer   = ctx.createBuffer(1, bufLen, ctx.sampleRate);
      const data     = buffer.getChannelData(0);
      for (let i = 0; i < bufLen; i++) data[i] = (Math.random() * 2 - 1);
      const src = ctx.createBufferSource();
      const gn  = ctx.createGain();
      src.buffer = buffer;

      if (def.filter) {
        // Shape the noise spectrum — transforms raw hiss into a physical material
        const flt = ctx.createBiquadFilter();
        flt.type            = def.filter.kind  || 'lowpass';
        flt.frequency.value = def.filter.freq;
        flt.Q.value         = def.filter.Q || 1.0;
        src.connect(flt);
        flt.connect(gn);
      } else {
        src.connect(gn);
      }
      gn.connect(dest);
      if (attack > 0) {
        gn.gain.setValueAtTime(0.0001, t0);
        gn.gain.linearRampToValueAtTime(def.vol, t0 + attack);
      } else {
        gn.gain.setValueAtTime(def.vol, t0);
      }
      gn.gain.exponentialRampToValueAtTime(0.0001, t0 + decayDur);
      src.start(t0);
      src.stop(t0 + def.dur + 0.002);

    } else if (def.type === 'fm') {
      // FM synthesis: modulator drives carrier frequency, giving a physical "bonk"
      const car    = ctx.createOscillator();
      const carGn  = ctx.createGain();
      const mod    = ctx.createOscillator();
      const modGn  = ctx.createGain();

      mod.frequency.setValueAtTime(def.carrier * def.ratio, t0);
      modGn.gain.setValueAtTime(def.depth, t0);
      modGn.gain.exponentialRampToValueAtTime(0.0001, t0 + def.dur);
      mod.connect(modGn);
      modGn.connect(car.frequency);   // FM: modulator → carrier's frequency input

      car.type = def.wave || 'sine';
      car.frequency.setValueAtTime(def.carrier, t0);
      car.connect(carGn);
      carGn.connect(dest);
      carGn.gain.setValueAtTime(def.vol, t0);
      carGn.gain.exponentialRampToValueAtTime(0.0001, t0 + def.dur);

      mod.start(t0); mod.stop(t0 + def.dur + 0.002);
      car.start(t0); car.stop(t0 + def.dur + 0.002);

    } else {
      // type === 'osc' — plain oscillator with frequency glide
      const osc = ctx.createOscillator();
      const gn  = ctx.createGain();
      osc.connect(gn);
      gn.connect(dest);
      osc.type = def.wave || 'sine';
      osc.frequency.setValueAtTime(def.freq, t0);
      if (def.endFreq) osc.frequency.exponentialRampToValueAtTime(def.endFreq, t0 + def.dur);
      gn.gain.setValueAtTime(def.vol, t0);
      gn.gain.exponentialRampToValueAtTime(0.0001, t0 + def.dur);
      osc.start(t0);
      osc.stop(t0 + def.dur + 0.002);
    }
  }

  function _play(layers, jitter = 0) {
    if (!_canPlay()) return;
    try {
      const ctx  = _getCtx();
      const dest = _getDest(ctx);
      const now  = ctx.currentTime;
      for (const def of layers) {
        const d = (jitter > 0 && !def.noJitter && (def.type === 'osc' || def.type === 'fm'))
          ? { ...def,
              freq:    def.freq    ? _jitter(def.freq,    jitter) : undefined,
              carrier: def.carrier ? _jitter(def.carrier, jitter) : undefined,
              endFreq: def.endFreq ? _jitter(def.endFreq, jitter) : undefined }
          : def;
        _layer(ctx, dest, now, d);
      }
    } catch (_) { /* audio not available */ }
  }

  // ── Sound design ─────────────────────────────────────────────────────────

  // ── Design rationale ────────────────────────────────────────────────────
  // Oscillators (even triangle) with pitch glide = alien/electronic.
  // Real clacks have NO sustained pitch — just shaped noise that dies fast.
  //
  // Three layers, all noise, all instantaneous onset (no attack ramp = snap):
  //   CLICK  — broadband, 0.5-1ms, lowpass 4kHz.  The hardest transient edge.
  //   BODY   — bandpass 1-1.5kHz, Q 0.7-0.9, 4-7ms.  The "ck" character.
  //   THUD   — lowpass 250-350Hz, 8-14ms.  Physical mass / board resonance.
  //
  // Punch = instantaneous onset + CLICK louder than everything else.
  // WHITE: higher bandpass (1300Hz), shorter thud → crisp tac
  // ── Design rationale ────────────────────────────────────────────────────
  // Q 0.8 bandpass = gentle spectral tilt = thud/thk with no clack character.
  // A real clack is a hard surface ringing briefly. To synthesise that:
  //   high-Q bandpass (Q 6-10) on a short noise burst → filter resonates at
  //   that frequency for ~Q/(π·freq) seconds. That ringing IS the clack tone.
  //   No oscillators, no pitch glide — just the filter's own resonance.
  //
  // Three layers:
  //   TRANSIENT  — 1ms, lowpass ≤2800 Hz (same ceiling for white and black)
  //   RESONANCE  — 6-10ms noise into high-Q bandpass → the clack ring
  //   WEIGHT     — lowpass 200-280Hz, gives physical mass
  //
  // White: resonance at 850 Hz, Q 8 → bright woody clack
  // Black: resonance at 560 Hz, Q 7 → darker heavier clack
  // Captures: same character, +30% vol, harder second event / scatter

  // DIFFERENTIATION STRATEGY:
  // White = crisp/snappy: bright slate (2900Hz), tk decays fast (14ms), light weight, no double-tap
  // Black = heavy/sustained: dark slate (2050Hz), tk decays slow (32ms), heavy weight
  // Captures: white has extra second snap hit + bright scatter; black has deeper bark + slower heavier scatter
  //
  // PICKUP: each sound starts with a short sine glide + tiny tk at t=0, landing 200ms before the impact.
  // White pickup: ascending (700→900 Hz, 65ms). Black pickup: descending (420→280 Hz, 65ms).

  // WHITE MOVE — crisp, snappy, bright bark
  const MOVE_WHITE = [
    // pickup (200ms before impact)
    { type: 'noise', dur: 0.002, vol: 0.22, decay: 0.008, filter: { kind: 'bandpass', freq: 1150, Q: 8  } },
    { type: 'osc',   wave: 'sine', freq: 700, endFreq: 900, dur: 0.065, vol: 0.09, noJitter: true },
    // impact at +200ms
    { type: 'noise', dur: 0.001, vol: 0.52, decay: 0.005, filter: { kind: 'bandpass', freq: 2900, Q: 12 }, delay: 0.200 }, // slate ring
    { type: 'noise', dur: 0.001, vol: 0.80, decay: 0.002, filter: { kind: 'lowpass',  freq: 2800 },        delay: 0.200 },
    { type: 'noise', dur: 0.006, vol: 0.68, decay: 0.012, filter: { kind: 'bandpass', freq: 1150, Q: 9  }, delay: 0.200 }, // tk body
    { type: 'noise', dur: 0.005, vol: 0.30, decay: 0.022, filter: { kind: 'bandpass', freq: 1050, Q: 15 }, delay: 0.203 }, // pitch bark
    { type: 'noise', dur: 0.005, vol: 0.16, decay: 0.008, filter: { kind: 'lowpass',  freq: 260  },        delay: 0.200 }, // weight
  ];

  // BLACK MOVE — heavy, sustained, low bark
  const MOVE_BLACK = [
    // pickup (200ms before impact)
    { type: 'noise', dur: 0.002, vol: 0.25, decay: 0.010, filter: { kind: 'bandpass', freq: 480,  Q: 7  } },
    { type: 'osc',   wave: 'sine', freq: 420, endFreq: 280, dur: 0.065, vol: 0.11, noJitter: true },
    // impact at +200ms
    { type: 'noise', dur: 0.001, vol: 0.55, decay: 0.007, filter: { kind: 'bandpass', freq: 2050, Q: 12 }, delay: 0.200 }, // slate ring
    { type: 'noise', dur: 0.001, vol: 0.90, decay: 0.004, filter: { kind: 'lowpass',  freq: 2800 },        delay: 0.200 },
    { type: 'noise', dur: 0.009, vol: 0.72, decay: 0.028, filter: { kind: 'bandpass', freq: 480,  Q: 7  }, delay: 0.200 }, // tk body
    { type: 'noise', dur: 0.007, vol: 0.38, decay: 0.040, filter: { kind: 'bandpass', freq: 500,  Q: 13 }, delay: 0.203 }, // pitch bark
    { type: 'noise', dur: 0.012, vol: 0.34, decay: 0.020, filter: { kind: 'lowpass',  freq: 220  },        delay: 0.200 }, // weight
  ];

  // WHITE CAPTURE — double-snap, bright scatter stretched to 3 waves
  const CAPTURE_WHITE = [
    // pickup
    { type: 'noise', dur: 0.002, vol: 0.22, decay: 0.008, filter: { kind: 'bandpass', freq: 1150, Q: 8  } },
    { type: 'osc',   wave: 'sine', freq: 700, endFreq: 900, dur: 0.065, vol: 0.09, noJitter: true },
    // impact at +200ms
    { type: 'noise', dur: 0.001, vol: 0.44, decay: 0.005, filter: { kind: 'bandpass', freq: 2900, Q: 12 }, delay: 0.200 }, // slate ring
    { type: 'noise', dur: 0.001, vol: 0.70, decay: 0.002, filter: { kind: 'lowpass',  freq: 2800 },        delay: 0.200 },
    { type: 'noise', dur: 0.006, vol: 0.62, decay: 0.014, filter: { kind: 'bandpass', freq: 1200, Q: 9  }, delay: 0.200 },
    { type: 'noise', dur: 0.006, vol: 0.22, decay: 0.010, filter: { kind: 'lowpass',  freq: 275  },        delay: 0.200 },
    { type: 'noise', dur: 0.006, vol: 0.36, decay: 0.030, filter: { kind: 'bandpass', freq: 1050, Q: 16 }, delay: 0.203 }, // capture bark
    // second snap
    { type: 'noise', dur: 0.001, vol: 0.25, decay: 0.004, filter: { kind: 'bandpass', freq: 2700, Q: 10 }, delay: 0.218 },
    { type: 'noise', dur: 0.001, vol: 0.38, decay: 0.002, filter: { kind: 'lowpass',  freq: 2600 },        delay: 0.218 },
    { type: 'noise', dur: 0.005, vol: 0.35, decay: 0.012, filter: { kind: 'bandpass', freq: 1100, Q: 7  }, delay: 0.218 },
    // scatter wave 1
    { type: 'noise', dur: 0.001, vol: 0.18, decay: 0.005, filter: { kind: 'bandpass', freq: 2600, Q: 10 }, delay: 0.265 },
    { type: 'noise', dur: 0.001, vol: 0.22, decay: 0.002, filter: { kind: 'lowpass',  freq: 2400 },        delay: 0.265 },
    { type: 'noise', dur: 0.004, vol: 0.18, decay: 0.012, filter: { kind: 'bandpass', freq: 1000, Q: 6  }, delay: 0.265 },
    // scatter wave 2
    { type: 'noise', dur: 0.001, vol: 0.13, decay: 0.004, filter: { kind: 'bandpass', freq: 2500, Q: 10 }, delay: 0.292 },
    { type: 'noise', dur: 0.001, vol: 0.16, decay: 0.002, filter: { kind: 'lowpass',  freq: 2200 },        delay: 0.292 },
    { type: 'noise', dur: 0.003, vol: 0.13, decay: 0.010, filter: { kind: 'bandpass', freq: 950,  Q: 6  }, delay: 0.292 },
    // scatter wave 3
    { type: 'noise', dur: 0.001, vol: 0.09, decay: 0.004, filter: { kind: 'bandpass', freq: 2400, Q: 10 }, delay: 0.322 },
    { type: 'noise', dur: 0.003, vol: 0.09, decay: 0.009, filter: { kind: 'bandpass', freq: 900,  Q: 6  }, delay: 0.322 },
  ];

  // BLACK CAPTURE — single heavy slam, low bark, slower heavier scatter stretched to 3 waves
  const CAPTURE_BLACK = [
    // pickup
    { type: 'noise', dur: 0.002, vol: 0.25, decay: 0.010, filter: { kind: 'bandpass', freq: 480,  Q: 7  } },
    { type: 'osc',   wave: 'sine', freq: 420, endFreq: 280, dur: 0.065, vol: 0.11, noJitter: true },
    // impact at +200ms
    { type: 'noise', dur: 0.001, vol: 0.42, decay: 0.007, filter: { kind: 'bandpass', freq: 2050, Q: 12 }, delay: 0.200 }, // slate ring
    { type: 'noise', dur: 0.001, vol: 0.70, decay: 0.005, filter: { kind: 'lowpass',  freq: 2600 },        delay: 0.200 },
    { type: 'noise', dur: 0.011, vol: 0.60, decay: 0.038, filter: { kind: 'bandpass', freq: 400,  Q: 7  }, delay: 0.200 },
    { type: 'noise', dur: 0.022, vol: 0.32, decay: 0.032, filter: { kind: 'lowpass',  freq: 195  },        delay: 0.200 },
    { type: 'noise', dur: 0.008, vol: 0.42, decay: 0.046, filter: { kind: 'bandpass', freq: 520,  Q: 14 }, delay: 0.203 }, // capture bark
    // scatter wave 1
    { type: 'noise', dur: 0.001, vol: 0.21, decay: 0.006, filter: { kind: 'bandpass', freq: 1950, Q: 10 }, delay: 0.288 },
    { type: 'noise', dur: 0.001, vol: 0.32, decay: 0.004, filter: { kind: 'lowpass',  freq: 2400 },        delay: 0.288 },
    { type: 'noise', dur: 0.008, vol: 0.27, decay: 0.028, filter: { kind: 'bandpass', freq: 400,  Q: 6  }, delay: 0.288 },
    { type: 'noise', dur: 0.014, vol: 0.18, decay: 0.022, filter: { kind: 'lowpass',  freq: 185  },        delay: 0.288 },
    // scatter wave 2
    { type: 'noise', dur: 0.001, vol: 0.15, decay: 0.005, filter: { kind: 'bandpass', freq: 1850, Q: 10 }, delay: 0.320 },
    { type: 'noise', dur: 0.001, vol: 0.22, decay: 0.003, filter: { kind: 'lowpass',  freq: 2200 },        delay: 0.320 },
    { type: 'noise', dur: 0.006, vol: 0.18, decay: 0.022, filter: { kind: 'bandpass', freq: 370,  Q: 6  }, delay: 0.320 },
    { type: 'noise', dur: 0.010, vol: 0.12, decay: 0.018, filter: { kind: 'lowpass',  freq: 175  },        delay: 0.320 },
    // scatter wave 3
    { type: 'noise', dur: 0.001, vol: 0.10, decay: 0.004, filter: { kind: 'bandpass', freq: 1800, Q: 10 }, delay: 0.352 },
    { type: 'noise', dur: 0.005, vol: 0.12, decay: 0.016, filter: { kind: 'bandpass', freq: 350,  Q: 5  }, delay: 0.352 },
    { type: 'noise', dur: 0.008, vol: 0.08, decay: 0.014, filter: { kind: 'lowpass',  freq: 165  },        delay: 0.352 },
  ];

  // ── Public API ──────────────────────────────────────────────────────────

  function move(isWhite) {
    _play(isWhite ? MOVE_WHITE : MOVE_BLACK, 0.07);
  }

  function capture(isWhite) {
    _play(isWhite ? CAPTURE_WHITE : CAPTURE_BLACK, 0.05);
  }

  /** Force-resume the AudioContext. Safe to call from a user-gesture handler. */
  function _ensureCtx() {
    if (!_ctx) {
      try { _ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (_) {}
    }
    if (_ctx && _ctx.state === 'suspended') _ctx.resume();
  }

  function toggle() {
    _enabled = !_enabled;
    localStorage.setItem('sound-enabled', _enabled);
    return _enabled;
  }

  return { move, capture, toggle, _ensureCtx, get enabled() { return _enabled; } };
})();
