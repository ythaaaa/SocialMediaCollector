# Third-party notices

## FFmpeg

The Windows release bundles an unmodified BtbN `win64 LGPL shared` FFmpeg
build and invokes `ffmpeg.exe` as a separate local subprocess for HLS media
remuxing. The packaged runtime consists of `vendor/ffmpeg/ffmpeg.exe` and the
seven shared FFmpeg DLLs beside it.

- Project: [FFmpeg](https://ffmpeg.org/)
- Provider: [BtbN/FFmpeg-Builds](https://github.com/btbn/ffmpeg-builds)
- Build: `n8.1.2-44-g7c533d0f86-20260823`
- Source revision: `g7c533d0f86`
- Variant: Windows x86_64, LGPL shared
- License: LGPL-3.0-or-later
- Archive SHA-256: `7B32983C242DD73D43C20836582572E9927986F8CB112AAFD62AA9978F4BE645`
- Archive provenance: see [`third_party/ffmpeg/SOURCE_INFO.txt`](third_party/ffmpeg/SOURCE_INFO.txt)
- Build details: see [`third_party/ffmpeg/BUILD_INFO.txt`](third_party/ffmpeg/BUILD_INFO.txt)
- Full license: [`third_party/ffmpeg/LICENSE.txt`](third_party/ffmpeg/LICENSE.txt)

No FFmpeg source modifications were made. The repository does not contain
the user's browser profile, cookies, tokens, passwords, or downloaded media.
This notice is an engineering distribution record, not legal advice; obtain a
final license review before public commercial distribution.
