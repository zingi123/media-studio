# MediaStudio – STEP 2 notes (Unified Audio Engine + Real-time Mixer)

## ស្ថាបត្យកម្ម (Architecture)
- **នាឡិកាតែមួយ** = ទីតាំង `QMediaPlayer` (video) ដែលមានស្រាប់។ Timeline, subtitle, canvas ទាំងអស់ធ្វើតាមវាដូចមុន។
- **Output សំឡេងតែមួយ** = `QAudioSink` មួយ (`AudioOutput` ក្នុង `app/ui/playback.py`) ដែលទទួល PCM ពី `MixEngine` (`app/studio/engine.py`, គ្មាន Qt)។
- `MixEngine` = cursor មួយ + `TrackSource` ≤ ៦ (np.memmap នៃ wav 44.1 kHz stereo ដែល render ពី project)។ រាល់ chunk (~១០ ms) វាអាន `Mixer` ផ្ទាល់ (volume/mute/solo/enable/master) → ដូច្នេះការផ្លាស់ប្តូរឮភ្លាម (+ buffer 150 ms)។ Gain ramp ក្នុង chunk ដើម្បីកុំឱ្យមាន click។
- សំឡេងដើមរបស់ video ត្រូវ mute ពេល engine លេង (កុំឱ្យឮពីរដង)។ ប្រសិនបើគ្មាន track ផ្សេងត្រូវលាយ ហើយ Original Audio = 0% → engine ទំនេរ ហើយ video លេងសំឡេងខ្លួនឯងដូចមុន STEP 2។
- Play/Pause/Stop/ចប់ video ទាំងអស់ → `playbackStateChanged` → audio ចាប់ផ្តើម/ឈប់ (ប្រភពតែមួយ)។ Seek/±10s/Jump cue → `_pb_set_pos` (video+audio ជាមួយគ្នា)។ ទៅទាល់ drift ៥០ ms ម្តង: ប្រសិនបើ audio ≠ video > 0.12 s → restart audio នៅទីតាំង video។
- **Render cache**: ពេលចុច Play ដំបូង (ឬក្រោយកែ) worker render ៦ track ម្តង (ប្រើ `build_voice_track`, `render_clip_track`, Demucs stems ដែលមានស្រាប់ → ដូច Export)។ Volume/Mute/Solo មិនបណ្តាលឱ្យ render ឡើងវិញទេ; Enable/Disable និងការកែ clip/cue បណ្តាលឱ្យ render។
- ឯកសារដើមមិនត្រូវបានកែ/លាយឡើយ (cache នៅ `work/<video>/playback/<sig>/`)។

## ៦ Track
`orig_vocals`=Original Audio (Demucs vocals បើមាន ឬសំឡេងពេញរបស់ video), `khmer_voice`=Khmer Voice (cue audio), `ai_voice`=AI Voice (**ថ្មី**, clip ផ្ទាល់ខ្លួន), `orig_bg`=Background Audio (Demucs no_vocals), `bgm`=Music, `sfx`=SFX។ Key ៥ ចាស់មិនប្តូរ → project ចាស់ load បាន (`enabled` default True, `ai_voice` default 100%)។

## ឯកសារដែលកែ
- NEW `app/studio/engine.py`, NEW `app/ui/playback.py`
- `app/studio/model.py` (6 track, `Channel.enabled`, solo មិនរាប់ track disabled)
- `app/studio/audio.py` (`extract_original`, `gather_inputs(ai_clips=, orig_wav=)`)
- `app/ui/mixer_panel.py` (checkbox Enable, ឈ្មោះប៊ូតុង), `app/ui/studio_timeline.py` (lane AI VOICE + ប៊ូតុង "+ AI Voice"; lane ចាស់ "AI VOICE" ប្តូរឈ្មោះ "KH VOICE")
- `app/ui/studio_integration.py` (ភ្ជាប់ engine; លុប preview player ៣០ វិនាទីដែលផ្អាក video), `app/ui/translator_page.py` (Play/Seek/Skip/Jump ឆ្លងកាត់ engine; ប៊ូតុង Stop; Export ដាក់ AI Voice), `app/ui/translator_widgets.py` (`TransportBar` ទទួលប៊ូតុង Stop ស្រេចចិត្ត)
- Tests: `tests/test_step2_audio.py`, `tests/test_step2_controller.py` · `STEP2_changes.diff`

## រត់ test
`python tests/test_step2_audio.py -v` និង `python tests/test_step2_controller.py -v` (ត្រូវការ numpy + ffmpeg, មិនត្រូវការ Qt)។
