# GE200 USB protocol notes (DRAFT, stage 2, static analysis only)

Status legend: **[S]** = derived from static analysis of the vendor editor, never seen on the wire.
**[V]** = verified on the user's own device (USB captures / descriptors).
Nothing here has been confirmed by observing traffic yet.

## Transport [V]
- USB composite device, VID 0x0483 / PID 0x5703, product string "GE200".
- Interface 0 = USB Audio, interface 3 = HID. No MIDI port.
- HID interface: interrupt IN 0x81 + interrupt OUT, wMaxPacket 64, interval 10 ms.
- Report descriptor: Generic Desktop, 64 x 8-bit Input + 64 x 8-bit Output, **no report ID**
  (raw 64-byte reports).
- The vendor editor opens the HID device with an overlapped read/write handle; the device path is passed
  to it as its command-line argument by the vendor launcher [S].

## Packet framing [S]
```
offset  0     1     2        3        4     5..LEN+3   LEN+4    LEN+5
        0xAA  0x55  LEN_lo   LEN_hi   CMD   data...    CRC_hi   CRC_lo
```
- `LEN` counts payload bytes including `CMD`. Total packet size = `LEN + 6`.
- CRC-16: polynomial 0x1021, init 0x0000, MSB-first, final XOR 0xFFFF, computed over
  bytes `[2 .. LEN+3]` (the LEN field plus payload). Stored big-endian.
- Packets larger than 64 bytes are presumably split across several 64-byte reports; the receive side
  accumulates into a byte queue and scans for 0xAA [S, mechanism unverified].
- Candidate hello packet: `AA 55 01 00 E0 35 E1` (CMD 0xE0, no data; CRC computed offline).

## Host -> device command codes seen in the editor's sender loop [S]
The editor runs a cyclic sender (state machine). Every builder copies a block of editor state into the
packet **only if it differs from a cached copy** ("push on change"). So these are all *write/set*
commands carrying payload, not read requests:

| Group | CMD codes |
|-------|-----------|
| Hello / request (no payload) | 0xE0 |
| Effect-module parameter blocks (len 5 seen) | 0x90 .. 0x9A |
| Other state blocks | 0x82, 0x83 (0x200-byte block), 0x87, 0x89 |
| Other state blocks | 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA6, 0xA8, 0xAA, 0xAB |
| Other state blocks | 0xE1, 0xE2, 0xE4, 0xE5 |

Cycle order: E0 -> 90..9A -> 82 -> A2 -> A0 -> A1 -> A3 -> A4 -> A6 -> A8 -> E1 -> E4 -> E5 -> 83 -> 87
-> 89 -> E2 -> AA -> AB -> (back to 90).

## Device -> host
- A reader thread reads up to 0x2800 bytes and dispatches on `byte[4]` (CMD), handlers covering
  0x81, 0x82, 0x85, 0x86, 0x89, 0x90-0x91, 0x93-0x99, 0xA0-0xA6, 0xA8-0xAB, 0xBB, 0xE1-0xE3, 0xE5 [S].
  Other codes are ignored.
- The code space is **symmetric**: most codes seen in the sender also appear in the receiver. A handler
  parses the payload into the editor's state buffer **and also into the "last sent" cache**, so the editor
  does not echo received state back to the device [S]. This suggests the device reports its state using
  the same CMD codes (e.g. after hello), but this is an inference, not an observation.
- Meaning of each code is not yet decoded.

## HID report layout [V]
Each 64-byte HID report (interrupt OUT 0x02 / interrupt IN 0x81, no report ID) is:
```
byte 0      chunk length N (1..63)
byte 1..N   chunk data  (a slice of the packet stream below)
rest        padding (ignore)
```
Packets longer than 63 bytes are split over several reports. The receiver concatenates the chunk
data of all reports and scans the resulting byte stream for `AA 55` frames.
Windows host note: the I/O buffer is 65 bytes (leading report-ID byte 0x00, then the 64-byte report).

## Observed on the wire [V]
- Hello: report `07 AA 55 01 00 E0 35 E1 00 ...` (so framing = length byte + frame).
  The first attempt WITHOUT the length byte got no reply (device ignores it); with it, the device
  immediately streams its whole state.
- Idle: no unsolicited traffic, and turning knobs does not produce HID traffic before a hello was sent.
- The reply to one hello (about 1.8k reports, ~105 KB of stream, finished in well under 1 s) was
  219 frames, **all passing the CRC above**:

| CMD | payload len (excl. CMD) | count | note |
|-----|-------------------------|-------|------|
| 0xA5 | 513 | 200 | one per preset: byte0 = preset index 0..199, then 512 bytes; name text near the start |
| 0x84 | 16 | 1 | |
| 0x85 | 150 | 1 | |
| 0x86 | 512 | 1 | |
| 0x89 | 1 | 1 | |
| 0x90..0x99 | 12,10,-,16,12,10,22,14,16,12 | 1 each | (0x92 absent) per-module/global blocks |
| 0xA1 / 0xA3 / 0xA4 / 0xA6 | 10 / 7 / 12 / 1 | 1 each | |
| 0xBB | 2 | 1 | |
| 0xE3 | 150 | 1 | |

  (payload lengths here are counts after the CMD byte; the LEN field is that count + 1.)
- Preset frame (CMD 0xA5): `A5 | idx | 01 02 03 04 05 06 07 08 09 (order table?) | ... | ASCII name ...`
  Names seen look like normal tone names in upper case; exact field offsets still to be mapped.

## Preset layout (CMD 0xA5 payload = `idx` + 512-byte preset) [V: derived from the 200-preset dump]
Offsets below are inside the 512-byte preset (after the idx byte).

| Offset | Size | Meaning |
|--------|------|---------|
| 0..8 | 9 | processing-chain order, a permutation of module numbers 1..9 |
| 9 | 1 | always 0 in the dump |
| 10 | 1 | 0x64 in the examples (a level-like value, meaning unconfirmed) |
| 12..27 | 16 | preset name, ASCII, NUL padded |
| 28..33 | 6 | module block for state CMD 0x90 |
| 36..40 | 5 | module block for state CMD 0x91 |
| 44..51 | 8 | module block for state CMD 0x93 |
| 52..57 | 6 | module block for state CMD 0x94 |
| 60..64 | 5 | module block for state CMD 0x95 |
| 68..75 | 8 | first part of the block for state CMD 0x96 |
| 76..81 | 6 | block for state CMD 0x97 (apart from its last 16-bit value) |
| 92..97 | 6 | block for state CMD 0x99 |
| ~325..352 | | little-endian 16-bit values (e.g. 0x0064, 0x0258, 0x04E2, 0x00DF, 0x0028, 0x00C8): the "wide" values of blocks 0x96-0x98 |
| rest | | mostly zero in the dump |

Each state block (CMD 0x90..0x99) in the dump is a list of 16-bit big-endian values; the **low
bytes equal the preset bytes with the first two values swapped** (0x90: state `00 01 32 32 32 32`
vs preset `01 00 32 32 32 32`). That mapping holds for 0x90, 0x91, 0x93, 0x94, 0x95 and 0x99 exactly.
What each module is (names, enable flag vs type id, parameter meanings) is **not yet decoded**.

## Module identification [V statistically + names from the editor's UI tables; ids/order inferred]
In the preset, a module block is `[type (1-based), enable (0/1), params...]`. The state frames carry
`[enable, type, params...]` (first two swapped). Evidence = value ranges across all 200 presets vs. the
number of entries in the editor's model-name tables:

| Preset offset | State CMD | Module | type range seen | params (value range seen) |
|---|---|---|---|---|
| 28 | 0x90 | FX (wah / compressor family) | 1..8 (8 models) | 4 x 0..100 |
| 36 | 0x91 | DS / OD | 1..20 (20 models) | 3 x 0..100 (gain/tone/level-like) |
| 44 | 0x93 | AMP | 1..65 (about 57 amps + 10 "EMPTY" user slots) | 6 x 0..100 (gain, bass, mid, treble, presence, master-like) |
| 52 | 0x94 | CAB | 1..35 (cab models + 10 IR slots) | mic 0..9 (10 mic models), distance-like 0..66, level-like 0..94, power-tube type 0..4 (off + 4 types) |
| 60 | 0x95 | NS (noise suppressor) | 1..3 (3 models) | 3 params |
| 68 | 0x96 | EQ | 1..4 (4 EQ types) | 6 band gains (0..24, 10 = flat-ish) + 3 wide 16-bit values (100, 600, 1250 in the sample = band frequencies) |
| 76 | 0x97 | MOD | 1..20 (20 models: phaser, flanger, chorus, ...) | 4 params + 1 wide 16-bit value |
| 84 | 0x98 | DELAY | 1..9 (9 delay types) | values at 86..91, time (ms) is a wide 16-bit value |
| 92 | 0x99 | REVERB | 1..7 (7 reverb types) | 4 params |

- The chain-order table at offsets 0..8 is a permutation of 1..9; the module ids are assumed to follow
  the block order above (1=FX ... 9=REVERB) -- assumption, not verified.
- State CMD 0x92 was not present in the dump; its role is unknown.
- Bytes 104..126 are mostly 0/1 flags (probably footswitch/EXP/control assignments, "CTRL" tab of the
  editor); bytes 128..136 hold a few more small values. Not decoded.
- Preset volume ("PRESET VOL" in the editor) is most likely the 0x64 at offset 10; unconfirmed.

## Parameter meanings per model [firmware-derived, cross-checked with real presets]
The device firmware (plain Thumb-2 code; readable, never modified or flashed) fills, for every effect model, four label
fields of its UI structure from small `case` blocks selected by the model number (`tbb` jump tables). The k-th label is
the k-th parameter byte of the module block (verified for the modulation module: the firmware copies the four bytes after
the type/enable pair into the four label fields in order). Results (`ge200/params.py` holds the tables):

| Module | Labels in byte order |
|---|---|
| DS | VOLUME, TONE, GAIN (all 20 models) |
| AMP | GAIN, BASS, MID, TREBLE, PRES, MST (all models) |
| REVERB | PRE DELAY, LEVEL, DECAY, TONE (all models) |
| FX | models 1-2: Q, POSITION, PEAK, LEVEL; 3-5: RATE, RANGE, PEAK, LEVEL; 6: ATTACK, SENS, PEAK, LEVEL; 7-8: ATTACK, THRES, RATIO, LEVEL |
| NS | 1: THRES; 2: SENS; 3: ATTACK, RELEASE, THRES (other bytes unused, default 20) |
| EQ | 1: 100 Hz, 250 Hz, 630 Hz, 1.6 kHz, 4 kHz (6th unused); 2: 80, 240, 750 Hz, 2.2, 6.6 kHz (6th unused); 3: 100, 200, 400, 800 Hz, 1.6, 3.2 kHz; 4 (custom): frequencies are separate 16-bit values |
| MOD | 1-3: RATE, LEVEL, DEPTH; 4-5: RATE, MIX, FEEDBACK; 6, 7, 11, 14: RATE, MIX, TONE; 8: RATE, DEPTH, TONE; 9, 10, 20: PITCH, MIX, TONE; 12-13: RATE, MIX, TONE, DEPTH; 15: RATE, MIX, Q; 16-17: RATE, MIX, RANGE; 18: RISE, LEVEL; 19: SAMPLE, MIX, BIT (unused bytes default to 50) |

Check against 165 real presets: 134 of 138 "used" knobs vary, 12 of 14 "unused" knobs stay at their default.

**Delay** (labels per model from the firmware; byte roles from its value handlers and from real presets):
p0 = LEVEL, p1 = F.BACK (editor label order, not proven), p2 = placeholder (always 0), p3 = SUB-D note division
(0 = off, 1 = 1/4, 2 = 1/4T, 3 = 1/4D, 4 = 1/8, 5 = 1/8T, 6 = 1/8D, 7 = 1/16, 8 = 1/16T, 9 = 1/16D; the strings table of
the editor lists exactly these ten), p4 = THRES (DYNAMIC, model 8 only), p5 = SUB B (TWO-HEAD, model 9 only; the firmware
copies byte 0x59 to SUB A, 0x5b to SUB B and the 16-bit value at 0x160 to TIME B).
**Cabinet:** MIC (0-9), CENTER, DISTANCE (order from the editor, not proven), POWER TUBE (0-4).

**16-bit values outside the module blocks** (little endian, offsets inside the 512-byte preset; located by matching the
device's state frames with the stored image, checked on 165 presets): 344/346/348 = custom EQ frequencies in Hz
(defaults 100/600/1250), 350 = delay time in ms (117..1066 seen), 352 = delay time B in ms (TWO-HEAD; 40 or 1000 in
other presets), 354 = a modulation value (200 by default, 63..305 seen; meaning unknown). Bytes 356..359 hold more 16-bit
values (1282, 1, 2, 2304 seen; unknown).
Not mapped: control/EXP assignment flags (bytes 104..126, 128..136), the 0x64 at offset 10 (preset volume?), offset 354.

## Other frames in the hello reply [V content, meaning inferred]
- 0x84: ASCII `2.0.4` + `MOOER_GE200` (firmware version + device name).
- 0x85 and 0xE3: 150 bytes each = ten 15-byte NUL/space-padded ASCII names (cabinet/IR style names);
  they look like lookup tables for the cabinet/IR selection.
- 0x86: 512 bytes, byte-for-byte equal to the stored preset of the active slot -> the **working copy**.
- 0xA6: 1 byte -> the index of the active preset.
- 0x89, 0xBB, 0xA1, 0xA3, 0xA4: small state blocks, not decoded.

## Writing module state (live edit) [V on the device, 2026-11-10]
- Host -> device `CMD 0x90..0x99` with the same payload layout the device reports (16-bit big-endian
  values `[enable, type, params...]`, 0x91 = 5 values) edits the **live state**. Example (DS/OD block, 0x91):
  `AA 55 0B 00 91 | 00 01 | 00 09 | 00 50 | 00 46 | 00 24 | 21 E8`.
- Sending the device's own reported values back is a no-op and gets **no reply/ack** (3 s listened).
- Test: switching the DS block off (`enable` 1 -> 0) was reflected immediately in the next hello reply:
  the 0x91 state frame and the live-buffer frame 0x86 changed, the stored preset (CMD 0xA5, same index)
  did **not**. Writing the original values back restored everything (verified by re-reading).
- So: these commands edit a working copy; persisting to a preset slot needs another command (not yet known;
  candidate: the 0x200-byte block sent by the editor's state 0x83 builder).

## Selecting a preset and loading a whole preset [V on the device, 2026-11-10]
- **Select preset**: host -> device `CMD 0xA6` + 1 byte (index). Example `AA 55 02 00 A6 A3 20 C6` selected
  preset 163. Verified by re-reading: the active-preset frame (0xA6) and the live buffer (0x86) followed,
  no stored preset changed, selecting the original index again restored everything. No ack frame.
- **Load whole live preset**: host -> device `CMD 0x83` + 512 bytes (a full preset image, no slot index).
  Verified on slot 199 (an "EMPTY" slot): after sending an image with one parameter changed, the **live buffer
  (0x86) equalled the sent image** while the **stored preset (CMD 0xA5 for that slot) was unchanged**. So 0x83
  edits the working copy, it does not save. (The vendor editor also overwrites the name field of this payload
  with the slot's existing name, so the name is probably not changeable this way.)
- **Write a preset into a slot**: host -> device `CMD 0xE5` + `[slot index][512-byte preset]` (the same layout
  as the device's own `0xA5` dump frame, LEN = 514). Found by static analysis of the editor's preset-import
  path (it parses the slot number from a preset file name and sends it followed by the 512 file bytes).
  Verified on slot 199: after sending an image with one parameter changed, the **stored preset** (CMD 0xA5 for
  that slot) equalled the sent image (byte 38: 50 -> 57); all other slots and the active preset were unchanged.
  Writing the original image back restored all 200 presets byte for byte. No ack frame.
- The editor's sender also uses 0xE0, 0x82, 0xA0-0xA4, 0xA8, 0xAA, 0xAB, 0xE1, 0xE2, 0xE4, 0x87, 0x89
  (0xA0 = the 9-byte chain order); these are not decoded yet.

## Timing and robustness notes [V on the device, 2026-11-10]
- The reply to a hello is **paced by the device**: about 8 ms per 64-byte report, ~12 presets per second, so a
  full state dump takes **about 16 s** (1831 reports). It cannot be requested in parts (no partial request known).
- Do not write while the device is streaming that dump. A write frame that collides with the dump can lose a
  report; the device's input parser then waits for the rest of the truncated frame and **ignores every later
  command, including hello**, until it has swallowed enough bytes. Recovery that worked: send ~11 chunks of
  63 zero bytes (`3F 00 ...` reports), then hello again. Writes are harmless again afterwards.
- A hello sent while a dump is still running restarts the dump (works, as long as nothing else is written).
- The device sends nothing unsolicited (no notification when a preset is changed on the unit itself): a host
  that wants to know the active preset has to ask with a hello (16 s).

## IR slots and amp import [S: static analysis of the editor; IR upload tried on a device, see below]
The preset's CAB model numbers 27..36 are the ten **user IR slots** (IR-1..IR-10 -> slot 0..9) and the amp models
56..65 are ten user amp slots ("EMPTY" until something is imported). The editor imports IRs from `*.wav` / `*.GIR`
and amps from `*.amp` / `*.GNR` files.

IR upload (`ge200/ir.py` implements the conversion and the frames). **[V]** One upload of a third-party cabinet IR (not part of this repository) (`ir-upload 9 file --name "MY CAB IR" --write`, 512-sample 24-bit file) was accepted by a real unit: the next hello reply showed the new name in entry 9 of name table 0x85 and nothing else in the tables changed. Whether the IR *sounds* right (level, polarity, which cab model number selects it) has not been judged by ear yet:
- Device audio rate is 44.1 kHz (USB audio descriptor [V]). An IR is **512 samples, 24-bit signed**. The editor takes the
  first 512 samples of the file unchanged (no resampling or normalisation seen), converts each with
  `trunc(clamp(x, +-0.99999899) * 2^23)` to 24-bit two's complement.
- Upload = four `CMD 0xE1` frames, payload `[slot 0..9][phase][data]`: phase 1 = the 512 high bytes, phase 2 = the 512
  middle bytes, phase 3 = the 512 low bytes (one byte per sample each), phase 4 = the 15-byte name (upper-case ASCII,
  NUL padded). LEN = 515 for phases 1-3, 18 for phase 4.
- Clear a slot: `CMD 0xE4` with the slot number (the editor then shows the name `EMPTY`).
- No acknowledgement frame is known; the IR names (ten 15-byte entries) are in the hello reply: table **0x85 = the ten user IR slots [V]**, 0xE3 = names that look like the ten user amp slots [S].
- Storage (static analysis of firmware 2.0.3, base address = 0x08000000 - 0x280 + file offset): the command dispatcher near 0x08001f9a compares the command byte with 0xE0/0xE1/0xE2/0xE4/0xE5; CMD 0xE4 (clear IR) erases 4 KB sectors at 0x50000 + (slot >> 1) * 0x1000, i.e. two IRs share a sector. The number of slots is therefore a firmware constant plus a storage layout, not something the protocol can change.

Amp import (`*.amp` / `*.GNR`) uses `CMD 0xE2` in phases as well (name up to 20 chars plus a data block). Those files are
Mooer's own capture format; its layout is unknown (no sample file analysed), and a NAM model is not that format.

## Editor start-up behaviour [S, partly confirmed by wire test]
- On start the sender state is initialised to 0xE0, so the hello is the editor's first packet; the
  device answers it with the full dump (confirmed above).

## Open questions
1. Does the device answer 0xE0 by streaming its current state (read-only dump)? Unknown.
2. Meaning/layout of each CMD's payload (parameter ids, preset number, name encoding).
3. Which codes are safe read requests vs. state writes. Until known: **no host -> device traffic**
   other than what is explicitly approved by the device owner.

## Safety
- Launching the vendor editor may push default/UI state to the device (push-on-change against an
  unknown initial cache) -- do not launch it against the device without a full backup.
- Never run the vendor launcher/updater (firmware flashing).
