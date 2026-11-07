# immich-upload

Upload folders of photos and videos to [Immich](https://immich.app), check that every
file arrived, and keep a log of which folders are done. Made for moving a photo
collection over bit by bit, with the option to delete each folder once it is safely in
Immich.

One Python file, standard library only (Python 3.9+). Works on Linux and Windows.
Written for and tested against the Immich v3.3.6 API.

## Setup

Create an API key in Immich under **Account Settings → API Keys** with these permissions:

| For | Permissions |
| --- | --- |
| Uploading | `asset.upload`, `asset.read`, `album.read`, `album.create`, `albumAsset.create` |
| `--tag` | `tag.read`, `tag.create`, `tag.asset` |
| `--date`, `--place` | `asset.update` |

Then set the server and key, or pass them as `--server` and `--api-key`:

```sh
export IMMICH_URL=https://photos.example.org
export IMMICH_API_KEY=...
```

On Windows: `setx IMMICH_URL https://photos.example.org` and `setx IMMICH_API_KEY ...`,
then open a new terminal.

## Usage

```sh
immich-upload.py FOLDER...                    # upload, album named after each folder
immich-upload.py -a "Iceland 2019" FOLDER     # choose the album
immich-upload.py -t Travel/Iceland FOLDER     # tag every file (nested tags with "/")
immich-upload.py --delete FOLDER              # delete the folder once all is verified
immich-upload.py -n FOLDER                    # dry run: show what would happen
immich-upload.py -L FOLDER                    # list the files, their dates and the plan
immich-upload.py --list-albums --list-tags    # what is in Immich
```

`immich-upload.py -h` lists all options.

## What a run does

For each folder:

1. **Find the files.** All photos and videos in the folder and its subfolders, using the
   file types the server accepts. An `.xmp` sidecar (`IMG_1.jpg.xmp` or `IMG_1.xmp`) is
   sent along with its photo.
2. **Skip what Immich has.** Every file is checksummed (SHA-1). Files Immich already has
   are not sent again.
3. **Check dates.** Each file to upload is checked for a capture date, using the same
   tags Immich reads. EXIF "ModifyDate" does not count. If a file has none, the folder
   stops before anything is uploaded, because Immich would date it by the file time.
   See [Dates and places](#dates-and-places).
4. **Upload and verify.** After uploading, every checksum must be in Immich, belong to
   the right asset, and not be in the trash.
5. **Album and tags.** All files go into the album: `--album`, else the album the log
   already has for the folder, else the folder's name. `--tag` tags are created if
   missing.
6. **Log.** The folder gets one row in the log, which is updated on every run.
7. **Delete (with `--delete`).** Only runs when everything checked out. It deletes the
   uploaded files, their sidecars and system junk (`Thumbs.db`, `desktop.ini`, ...),
   then the folder if it is empty. Other files are kept and listed.

A run is safe to repeat or interrupt. Files that made it are not sent again.

## Dates and places

Scans and photos from messengers often have no capture date or GPS position. For those:

```sh
immich-upload.py --date 1998-08 --place "Rome, Italy" "Scans/Rome 1998"
```

- **`--date`** takes `1998`, `1998-08`, `1998-08-14` or `"1998-08-14 15:30"`, optionally
  with a UTC offset. Without a time it uses 12:00; without an offset, this computer's
  time zone. The files get times one second apart in file name order, so scans keep
  their order in the timeline.
- **`--date-from-name`** finds the date in names instead, in two steps:
  1. **Folder name.** A folder named like `2019-07-14 Iceland`, `2019-07 Iceland`,
     `Rome 1998` or `2005-03-04 - 2005-03-06 Paris` dates the undated files in it and
     its subfolders. This only happens if no file in it has an EXIF date outside that
     day, month, year or range (give or take a day). The files get noon of the first
     day, one second apart. A file whose own name has a date inside the folder's period
     gets that date instead, as it is more precise. Since a folder name is only a rough
     date, the script lists these files and asks before uploading (`--force`: no
     question).
  2. **File name.** The files no folder dated get the date in their file name:
     `IMG_20190714_153012`, `PXL_20190714_153012345`, `IMG-20190714-WA0001` (WhatsApp,
     date only: noon, one second apart), `2019-07-14 15.30.12`, ... This only happens if
     every one of those files has a date in its name or EXIF, and no EXIF date
     contradicts its name.

  Times in names are taken as this computer's time zone. What a name can't date,
  `--date` covers, if given. The output says which step was used, or why not.
- **`--place`** takes coordinates (`"41.9, 12.5"`) or a place name, which is looked up
  on OpenStreetMap (only the name is sent). The match is printed so you can check it.
- Both are set **in Immich**, after the upload, and then read back. The files
  themselves are never changed. Immich stores the edit in its database and in an
  `.xmp` next to its own copy, and fills in the city and the timeline position itself.
  The local files keep their checksums, so a later run still recognises them as
  uploaded.

The rules:

- **Only files not yet in Immich are affected.** Files Immich already has are not
  checked for a date and keep their date and place.
- **Only files without a date or place get one.** If some of the files have one and
  others don't, the script lists them and asks first, then sets it only on the others.
  Without a terminal to ask in, the answer is no.
- **`--force` is a re-upload.** It also redoes folders logged as verified, and
  `--date`/`--place` then apply to every file in the folder, including those already
  in Immich, overwriting what they have, without asking.
- **`--allow-undated`** uploads undated files anyway.

Files the date reader cannot look into (GIF, AVI, MKV, BMP, ...) count as undated.
The reader handles JPEG, PNG, WebP, TIFF, HEIC/AVIF, MP4/MOV, the common raw formats
(DNG, CR2, CR3, NEF, ARW, RAF, RW2, ...) and XMP.

## Checking a folder first

`-L` / `--list-files` changes nothing. It lists every file with its date as stored, the
tag the date came from, its GPS position and camera, and what a run with the same
options would do:

```
File          Size   Taken                Date from              Location             Camera  Plan
------------  -----  -------------------  ---------------------  -------------------  ------  ----------------------------------------
dated.jpg     397 B  2019-07-14 15:30:00  EXIF DateTimeOriginal  -                    -       upload, set place
gps.jpg       511 B  2018-01-02 03:04:05  EXIF DateTimeOriginal  64.14660, -21.94260  -       in Immich, left as is
zerodate.jpg  397 B  -                    -                      -                    -       upload, set date 1990-01-01 12:00:00, set place
```

With a server set, it also checks which files Immich already has. That means reading
and checksumming every file. Without a server it only reads the files locally.

## The log

By default the log is `Documents/immich-uploads.md` in your user folder: on Windows the
real Documents folder, on Linux the XDG documents folder. Set `--log` or
`IMMICH_UPLOAD_LOG` to use another file. It is a Markdown table with one row per folder:

| Folder | Album | Tags | Files | Uploaded | Status | Deleted | First run | Last run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `D:\Photos\2019 Iceland` | 2019 Iceland | Travel/Iceland | 412 | 412 | verified | yes | 2026-11-07 15:02 | 2026-11-07 15:02 |

- **Uploaded** adds up the files this script sent over all runs.
- **Status** is `verified`, `N failed`, `album incomplete`, `tags not set`,
  `date/place not set` or `stopped: N without date`.
- A `verified` folder is skipped next time, unless its number of files changed or
  `--force` is given.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | every folder checked out |
| 1 | a folder had a problem, stopped for missing dates, or was not confirmed |
| 2 | setup problem: server not reachable, key refused |
| 130 | interrupted with Ctrl-C |
