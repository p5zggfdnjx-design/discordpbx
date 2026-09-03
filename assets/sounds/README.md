# DiscordPBX bundled sound pack

These files are runtime-optimized Opus transcodes of the audio assets supplied by the project owner for DiscordPBX. They are intentionally committed with the application so installs and managed updates do not depend on an external sound host.

| Event | Bundled file | Original uploaded file | Original SHA-256 |
| --- | --- | --- | --- |
| Discord voice joined | `start-call.opus` | `start-call.mp3` | `16ef9eb6b431a049a3b5217545043d521d05a5bb2a6e90911cafcd53ea0e9420` |
| Incoming call | `phone-ring.opus` | `phone-ring.mp3` | `f714d6f89bc30540210aa7caad92e46e1a6bd5b446ec9818f76cb998dc0a98db` |
| Outbound ringing | `call-ring.opus` | `call-ring.mp3` | `f45963fc77453dd3b3429796688b5d95225b57f36f4decdd6b78d92ec35d27c2` |
| Hold | `hold-call.opus` | `hold-call.mp3` | `644201826f6f0bef4f1eda9e30f19d65dea013ad901edd0440d8b1ab581ef26e` |
| Declined/cancelled | `call-declined.opus` | `call-declined.mp3` | `d702fdf1709f1be70067daaed41a2346f9c6ddd6e3e8bfe6472dd7b8807dddf5` |
| Hangup | `hangup.opus` | `hangup.mp3` | `d411af4bba911d56e295188ba949ae0726a0cc10c22af2d6ce112b910ecf29eb` |
| Call failure | `call-failed.opus` | `call-failed.mp3` | `0bb67a830810367e93748f1c41cbe881d04f15ce55cb7d9b518cc056fcd78f12` |

Runtime decoding is performed once at process startup by ffmpeg into Discord's 48 kHz, 16-bit stereo PCM format. The decoded frames are queued through the bridge's Discord-local alert mixer, so these UI cues are not transmitted to the PSTN/PBX caller.
