# immich-upload

Move photo folders to [Immich](https://immich.app) bit by bit, safely:

- uploads a folder with its subfolders and `.xmp` sidecars, skipping what Immich already has
- checks afterwards that every file is in Immich under its exact checksum
- stops before uploading files without a capture date, which Immich would date by the
  file time, and can fill in dates and places for them
- logs each folder and can delete it once everything is verified

One Python file, standard library only (Python 3.9+), Linux and Windows. Written for the
Immich v3 API.

## Quick start

```sh
export IMMICH_URL=https://photos.example.org   # Windows: setx IMMICH_URL ..., then a new terminal

immich-upload.py -n FOLDER            # look first: each file's date, place and what would happen
immich-upload.py FOLDER               # upload into an album named after the folder
immich-upload.py --delete FOLDER      # ... and delete the folder once all is verified
```

Create the API key in Immich under **Account Settings → API Keys** with these permissions:

- `asset.read`
- `asset.update` (only for `--date`/`--place`)
- `asset.delete` (only for `--restore-trashed`)
- `asset.upload`
- `album.create`
- `album.read`
- `albumAsset.create`
- `tag.create` (only for `--tag`)
- `tag.read` (only for `--tag`)
- `tag.asset` (only for `--tag`)

Save the key in a file only you can read. The script reads it from this default path
by itself, and the key never shows up in your shell history or the process list:

- Linux: `~/.config/immich-upload/api-key` (or `$XDG_CONFIG_HOME/immich-upload/api-key`)
- Windows: `%APPDATA%\immich-upload\api-key`

```sh
mkdir -p ~/.config/immich-upload
(umask 077; read -rsp "API key: " k && printf '%s\n' "$k" > ~/.config/immich-upload/api-key)
```

```powershell
# Windows (PowerShell)
$k = Read-Host "API key" -AsSecureString
New-Item -Force -ItemType Directory "$env:APPDATA\immich-upload" | Out-Null
[Net.NetworkCredential]::new("", $k).Password | Set-Content -Encoding ascii "$env:APPDATA\immich-upload\api-key"
```

For a key file somewhere else, use `--api-key-file FILE` or `IMMICH_API_KEY_FILE`. `IMMICH_API_KEY` and
`--api-key` work too, but are easier to leak.

`-n` changes nothing and shows what a run with the same options would do. With a server
set, it also checks the key and which files Immich has; without one, it works offline:

```
== D:\Photos\2019 Iceland
  Album '2019 Iceland' (new), tag Travel
  File       Size   Taken                          Date from              Location                Camera  Plan
  ---------  -----  -----------------------------  ---------------------  ----------------------  ------  --------------------------------------------------
  dated.jpg   87 B  2019-07-14 15:30:00            EXIF DateTimeOriginal  -> 64.14660, -21.94260  -       upload, keeps its date (matches --date), set place
  gps.jpg    201 B  2019-07-15 03:04:05            EXIF DateTimeOriginal  64.14660, -21.94260     -       in Immich, add to album and tags
  scan.jpg    13 B  -> 2019-07-01 12:00:00 +02:00  --date                 -> 64.14660, -21.94260  -       upload, set date, set place
  3 files (301 B), taken 2019-07-14 15:30 to 2019-07-15 03:04; 1 already in Immich, 2 to upload
  Dates, of them: 1 get --date; 1 keep a date matching --date
  Place, of them: 2 files get 64.14660, -21.94260
  --delete: once all 3 are verified, deletes them; then the folder
```

## Options

| Option | |
| --- | --- |
| `-a ALBUM` | album name (default: the one in the log, else the folder's name) |
| `-t TAG` | tag every file; repeatable; nested with `/`, e.g. `Travel/Iceland` |
| `--date DATE` | date for files without one: `1998`, `1998-08`, `1998-08-14`, `"1998-08-14 15:30"` |
| `--date-from-name` | take missing dates from folder or file names (see below) |
| `--place PLACE` | place for files without GPS: `"41.9, 12.5"` or a name, looked up on OpenStreetMap |
| `--allow-undated` | upload files without a date anyway |
| `--restore-trashed` | restore files whose copy in Immich is in the trash (else they count as failed) |
| `--delete` | delete uploaded files, sidecars and junk once all is verified; other files stay |
| `-n` (or `-L`) | dry run: list the files and what would happen; changes nothing |
| `-f` | force: redo a verified folder as a re-upload (see below) |
| `--list-albums`, `--list-tags` | list the albums or tags in Immich |
| `--api-key-file FILE` | read the API key from FILE instead of the default path |

`immich-upload.py -h` shows all options.

A run is safe to repeat or interrupt. A folder logged as verified is skipped, unless
its number of files changed. Files Immich already has are not sent again, but are added
to the album and tagged.

## Dates and places

`--date` and `--place` are set in Immich after the upload; the files are never changed,
so they keep matching by checksum. They apply only to files that are not in Immich yet
and lack a date or place:

- **Mixed folders ask first.** If some files have a date and others not, the script lists
  them and asks. A date that is the `--date` value or more precise doesn't count:
  `--date 2019-07` leaves a photo from 2019-07-14 alone without asking.
- **Times.** Without a time, `--date` means 12:00 (this computer's time zone). Files are
  set one second apart in file name order, so scans keep their order.
- **`--force`** applies them to every file, also those already in Immich, overwriting
  their places and any dates that don't match, without asking.

`--date-from-name` tries two steps; the output says which one dated a file, or why not:

1. **Folder name** (`2019-07-14 Iceland`, `2019-07 Iceland`, `Rome 1998`,
   `2005-03-04 - 2005-03-06 Paris`): dates the undated files in it, if no file in it has
   an EXIF date outside that period. A file name with a date inside the period wins. As
   a folder name is only a rough date, the script asks first.
2. **File name** (`IMG_20190714_153012`, `PXL_…`, `IMG-20190714-WA0001`,
   `2019-07-14 15.30.12`): only if every remaining file has a date in its name or EXIF,
   and no EXIF date contradicts its name.

`--date` covers what's left. Without a terminal to ask in, the answer is no.

The date reader looks at the same tags Immich does (EXIF ModifyDate doesn't count) in
JPEG, PNG, WebP, TIFF, HEIC, MP4/MOV, common raw formats and XMP. Other types, such as
GIF or AVI, count as undated.

## Log

`Documents/immich-uploads.md` (or `--log FILE`, `IMMICH_UPLOAD_LOG`), one row per folder:

| Folder | Album | Tags | Files | Uploaded | Status | Deleted | First run | Last run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `D:\Photos\2019 Iceland` | 2019 Iceland | Travel/Iceland | 412 | 412 | verified | yes | 2026-11-07 15:02 | 2026-11-07 15:02 |

Exit codes: 0 all verified, 1 a folder failed, stopped or wasn't confirmed, 2 server or
key problem, 130 interrupted.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md). Copyright (C) 2026 Tobias Mayr, licensed under
the [GPL v3 or later](LICENSE).
