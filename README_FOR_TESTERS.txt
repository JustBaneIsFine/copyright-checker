DJ Copyright Prep - Tester Build
================================

What it does
------------
Point it at a folder of songs and it builds ONE small file that contains all the
songs' audio combined - made to upload fast and let YouTube's Content ID recognise
the tracks. You can output either a tiny still-image video (for YouTube) or just the
combined audio file.

How to run (Windows)
--------------------
1. Unzip the whole DJCopyrightPrep folder somewhere (Desktop is fine).
   Keep all the files together - the app needs the folder next to the .exe.
2. Open the folder and double-click  DJCopyrightPrep.exe  (it starts in about a second).
   - If Windows SmartScreen shows "Windows protected your PC", click "More info" ->
     "Run anyway". (The app isn't code-signed yet, which is expected for a test build.)
   - If you don't have Microsoft Edge WebView2, the app installs it automatically the
     first time (needs an internet connection for that one-time step).

3. In the window:
   - Click "Choose folder" and select a folder containing your songs
     (.mp3 .wav .flac .m4a .aac .ogg).
   - Pick options:
       * Output:  "Video + Audio"  -> a tiny .mp4 (upload this to YouTube)
                  "Audio only"      -> just the combined .wav audio file
       * "Trim each song" (on by default) grabs ~20 seconds from ~30s into each track
         to keep the file small. Turn it off to use full songs.
       * Audio quality (defaults to 64 kbps, which is plenty for recognition).
   - Click Generate. Watch the progress bar.
   - When done, the output file is saved INSIDE the folder you chose, named
     "<foldername>_copyright.mp4" (or .m4a). Click "Open folder" to find it.

What to test / report back
--------------------------
- Did the window open and look right (centered, readable)?
- Did Choose folder work?
- Did both "Video + Audio" and "Audio only" produce a file?
- Try a folder with many songs, and one with a very short song.
- Roughly how long did it take, and how big was the output?
- Anything confusing, broken, or ugly.

If something goes wrong
-----------------------
There's a log file here that helps me debug:
   %LOCALAPPDATA%\DJCopyrightPrep\app.log
(Paste it into the file explorer address bar, or send me that file.)

Thanks for testing!
