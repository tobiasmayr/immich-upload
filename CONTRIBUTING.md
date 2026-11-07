# Contributing

How `immich-upload.py` is built, and how to change it without breaking what users rely on.
For what the script does, see the [README](README.md).

## Ground rules

- **One file, standard library only.** It must run with a plain Python 3.9+ on Linux and
  Windows, copied onto a machine without installing anything. No third-party imports in
  the script. Tests may use pytest.
- **Never change the user's files.** Dates and places are set in Immich, not in the
  files: an edited file gets a new checksum, so Immich would store it a second time, and
  sync tools (Dropbox, Nextcloud, ...) would spread the edit.
- **Delete only what is verified.** `--delete` runs only when every file of the folder
  is in Immich under its exact checksum and not in the trash, and album, tags and
  date/place all worked. It removes media, their sidecars and known junk, never other
  files.
- **Files already in Immich are left alone**, unless `--force` is given (a re-upload).
- **Ask before guessing.** When a date would be applied to a folder where some files
  have dates and others not, or comes from a folder name only, the script asks. Without
  a terminal the answer is no. `--force` skips the question.
- **Safe to rerun and to interrupt.** A rerun picks up where the last one stopped.

## Layout of the script

The sections of `immich-upload.py`, in order:

| Section | What it does |
| --- | --- |
| API | `Immich`: HTTP client (streaming multipart upload, no dependencies) |
| photo dates and GPS | `read_meta()`: a small reader for EXIF, XMP, PNG, WebP, ISO BMFF (HEIC, MP4, MOV, CR3) and RAF |
| files | `scan()`: media, sidecars, junk and other files of a folder |
| log | `Log`: the Markdown log, one row per folder |
| main work | `process()`: one folder, from scan to delete |
| --date / --place | `in_immich()`, `meta_plan()`, `dates_from_names()`, `plan_ok()`, `set_date_place()` |
| (after that) | option parsing, `--list-albums`, `--list-tags`, the dry run (`preview()`), `main()` |

`meta_plan()` decides everything about dates and places for a folder. Both the upload
and the dry run use it, so the dry run always shows what a run would do.

## Immich API

Endpoints used (v3; the URL gets `/api` appended if missing):

| Endpoint | For |
| --- | --- |
| `GET /server/media-types` | which file types to upload |
| `POST /assets/bulk-upload-check` | which checksums Immich has (before and after upload) |
| `POST /assets` | upload (multipart: `assetData`, `sidecarData`, `fileCreatedAt`, `fileModifiedAt`, `filename`; header `x-immich-checksum`) |
| `GET /albums?name=`, `POST /albums`, `PUT /albums/{id}/assets` | albums |
| `GET /tags`, `PUT /tags`, `PUT /tags/assets` | tags |
| `PUT /assets/{id}` `{dateTimeOriginal}` | `--date`, one asset at a time |
| `PUT /assets` `{ids, latitude, longitude}` | `--place`, in bulk |
| `GET /assets/{id}` | reading date and place back |

Immich keeps date/place edits in its database and writes them to an `.xmp` next to its
own copy of the file. The original keeps its checksum.

## The date reader

`read_meta()` has to agree with Immich about which files have a capture date. Otherwise
the "stop on undated files" check is wrong. It follows Immich's own list in
`server/src/services/metadata.service.ts`:

- **Counted:** DateTimeOriginal, CreateDate, the GPS date, QuickTime creation dates, and
  the XMP equivalents, embedded or in the sidecar.
- **Not counted:** EXIF ModifyDate, and zero dates.

When adding a format or tag, check that list first.

Formats the reader cannot look into return `readable=False` and count as undated.
Besides the yes/no answer, `Meta` also carries what the dry run shows: the full
timestamp (`when`), where the date came from (`src`), the coordinates and the camera.

## Tests

The tests use [pytest](https://pytest.org) and need no Immich server:

```sh
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/pytest test
```

| File | Covers |
| --- | --- |
| `test/media.py` | not tests: builds tiny JPEG, PNG, WebP, TIFF, MP4 and MOV files with exactly the EXIF, XMP or QuickTime data a test needs (`salt=` makes the checksums differ) |
| `test/fake_immich.py` | not tests: an in-process stand-in for the endpoints above. It keeps assets by SHA-1 like Immich, has trash, albums, tags and date/place edits, and `fail_names` makes uploads of given file names fail |
| `test/conftest.py` | loads the script as a module, starts the fake server, and provides `run(...)`, which calls `main()` with arguments and returns `(exit code, output)` |
| `test_reader.py` | the date/GPS reader |
| `test_dates.py` | `--date` parsing and matching, `--place`, `--date-from-name` |
| `test_log.py` | the Markdown log |
| `test_upload.py` | whole runs against the fake server |

Questions are answered through `run(..., answer=True/False)`. Without `answer`, a
question fails the test, so a new question can't sneak in unnoticed. `run.questions`
holds what was asked.

When changing behaviour, add or change a test in `test_upload.py` that shows it from
the user's side: files in a folder, a command line, and what ends up in the fake Immich
and the log.

The fake server is not Immich. Before relying on a new endpoint or field, check it
against the real API spec, and test against a real server once.

## Before committing

- `.venv/bin/pytest test` passes.
- `./immich-upload.py -h` still reads well.
- `README.md` describes any change users will notice. This file describes changes to
  how things work inside.
- The docstring at the top of the script matches the behaviour.
