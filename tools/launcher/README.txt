SPCX MORNING - desktop launcher
===============================

WHAT THIS IS
------------
One icon on the Desktop that opens a small menu, so a week of running
things by hand does not mean remembering seven commands.


SETTING IT UP  (once)
---------------------
1. Put these two files in C:\Dev\Feed-Check, beside the Python scripts:

       SPCX Morning.bat
       Create Desktop Icon.ps1

   They have to live in the same folder as open_candles.py and
   spcx_alert.py. Those scripts import each other, so splitting them up
   breaks them.

2. Open PowerShell in that folder and run:

       powershell -ExecutionPolicy Bypass -File ".\Create Desktop Icon.ps1"

   It hunts for your Market Scanner image, converts it to an icon if it
   is a .png, and puts "SPCX Morning" on the Desktop.

   If it cannot find the image it lists what it did find, so you can
   copy a path and run:

       powershell -ExecutionPolicy Bypass -File ".\Create Desktop Icon.ps1" -Image "C:\path\to\icon.png"

3. Double-click the Desktop icon.


THE MENU
--------
  1  Start the morning    both programs, two windows
  2  Candles only         08:55 to 10:00
  3  MACD alerts only     logs from 09:30, alerts from 09:40

  4  Test my phone        one quiet notification, one alarm
  5  Replay a past day    full SIP tape, costs nothing

  6  Today's signals      what has fired so far
  7  End of day           fill in what price did next, then show it


A NORMAL WEEK
-------------
  Morning   double-click the icon, choose 1, leave it
  Evening   double-click the icon, choose 7

Option 7 is the one that matters. It records what price did over the
next 15, 30 and 60 minutes after every signal, and that table is what
tells us which alerts were worth your attention.


BEFORE THE FIRST MORNING
------------------------
Run option 4 and check your phone. You should get TWO notifications:
the first silent, the second audible. If they behave any other way,
say so - the whole design rests on that difference.

The launcher refuses to start if .env is missing, and says which keys
it needs. It never prints their values.


IF THE ICON LOOKS GENERIC
-------------------------
Windows caches icons. Rename the shortcut, or log out and back in.
