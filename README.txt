ZERO HOUR MATCH READER
======================

WHAT IT DOES
Zero Hour saves your last few matches on your PC. This program reads that one file and sends new
matches (map, kills, deaths, win or loss, and your Steam name) to the community stats bot, so your real
kills and losses show up there. It does not touch the game and never sees your password.

SET UP (volunteers, running the Python script)
1. Install Python from python.org (tick "Add Python to PATH" in the installer). Nothing else is needed.
2. Keep all the files from this folder together.
3. Double-click start_reader.bat. The first time it asks you to type YES to allow sending your match results.
4. Leave the window open while you play. It checks every 20 seconds and sends each new match.

TO CHECK WHAT IT FOUND WITHOUT SENDING ANYTHING
Open a command window in this folder and run:   python zh_reader.py --dry-run

IF IT CAN'T FIND YOUR SAVE FILE
Play one match first. If it still can't find it, open reader_config.json and put the folder that
contains savedMH.file after "SaveFolder", using double backslashes, for example:
    "SaveFolder": "C:\\Users\\you\\AppData\\LocalLow\\SomeCompany\\ZeroHour"

TO STOP SHARING
Close the window. To remove your data from the bot, ask the bot's owner.

MAKING THE .EXE (bot owner, recommended for volunteers)
   Fill in reader_config.json (step 2 below), then on a Windows PC with Python installed,
   double-click build_exe.bat. It produces ZeroHourReader.zip: send that to volunteers.
   They need nothing installed; they just unzip it and double-click ZeroHourReader.exe.

SET UP (bot owner, before handing this folder out)
1. On the bot host, add two variables (the Variables page or the .env file next to start.py):
       ZH_INGEST_KEY=your-long-random-text
       INGEST_PORT=the-port-your-host-gave-you
   Restart the bot. The console should say "[ingest] Upload endpoint is listening on port ...".
   (If it says the endpoint is OFF, the key or the port is missing.)
2. In reader_config.json, set UploadUrl to the bot's address and that port, for example
       "UploadUrl": "http://your-host-address:PORT/api/ingest"
   and set ApiKey to the same key. Give the folder to volunteers with those two values filled in.
3. Anyone who has the key can upload, so change it (on both sides) if it leaks.

NOTE: the numbers come from a file on each player's own PC, so they are self-reported.
