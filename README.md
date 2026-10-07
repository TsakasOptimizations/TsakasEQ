# TsakasEQ

System-wide EQ for Windows, for any headphones, IEMs or speakers. Works in every app and game (Valorant, Spotify, Netflix…).

- Curve Style: drag the dots up/down, you hear it instantly
- **Apply** keeps it; closing without Apply goes back to the last applied sound
- Presets: **FPS Games** (footsteps/utility/direction), **Music**, **Movies**, **Flat**, plus your own
- **AI headphone profiles**: type your headphones/IEMs, TsakasEQ finds them in 8,800+ lab measurements ([AutoEq](https://github.com/jaakkopasanen/AutoEq)), corrects their sound and builds **AI FPS**, **AI Music** and **AI Movies**
- EQ keeps working with the app closed
- Built-in update check (GitHub releases)

## Install

Download `TsakasEQ.exe` from [Releases](https://github.com/TsakasOptimizations/TsakasEQ/releases) and run it. On the first launch it asks for admin once to set up its audio engine. If you don't hear the EQ after setup, restart your PC.

Windows SmartScreen may warn because the exe isn't code-signed: **More info → Run anyway**.

## Uninstall

Settings → Apps → uninstall **Equalizer APO** (restores your audio devices), then delete `TsakasEQ.exe` and `%APPDATA%\TsakasEQ`.


## Credits

Audio engine: [Equalizer APO](https://sourceforge.net/projects/equalizerapo/) by Jonas Thedering (GPL-2.0), bundled unmodified. Source: https://sourceforge.net/p/equalizerapo/code/
