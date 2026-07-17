; Custom NSIS header — electron-builder auto-includes build/installer.nsh into
; the installer script BEFORE MUI2 inserts the finish page, so these top-level
; defines are honoured by MUI_PAGE_FINISH (the standard assisted-installer finish
; page). This adds a clickable attribution link to the finish screen without
; touching the "Run Library Tool" checkbox (wired separately via runAfterFinish
; -> MUI_FINISHPAGE_RUN), which keeps working untouched.
!define MUI_FINISHPAGE_LINK "by Andrew Miller"
!define MUI_FINISHPAGE_LINK_LOCATION "https://maj-6.github.io/library-tool/"
; match the app's green branding instead of the default navy link colour
!define MUI_FINISHPAGE_LINK_COLOR "3F5F52"
