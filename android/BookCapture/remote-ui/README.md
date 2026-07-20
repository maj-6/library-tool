# Remote Android UI catalog

`catalog.json` is the desktop-editable source for the small UI overlay fetched
when **Check for updates** is pressed in Library Tool Capture.

- `strings` maps an Android string-resource name (or a view/menu ID) to text.
- `icons` maps an in-app `ImageView`, `MaterialButton`, or menu ID to a PNG
  path, relative to the catalog file. PNGs are size-limited, hashed during
  publishing, and verified again by the app before decoding.
- Increment `revision` for every published change.

Validate locally:

```text
python tools/android_ui_catalog.py check
```

Publish with the account currently signed into the desktop app:

```text
python tools/android_ui_catalog.py push
```

For a packaged desktop installation, pass `--data-root` (or set
`WHL_DATA_ROOT`) to its Library Tool data directory. The account must be listed
in the cloud `android_ui_publishers` table. Routine publishing uses the user's
session; it does not use a service-role key.

The installed launcher icon cannot be changed this way. Android treats that as
part of the signed APK, so `tools/make_android_icon.py` remains its build-time
manager.
