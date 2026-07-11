# Windows code signing

The installer is signed so Windows shows a real publisher instead of "unknown
publisher." Signing is driven entirely by environment/secret values — no key
material lives in this repo. The only file here is the **public** root CA
certificate (`whl-code-root-ca.crt`), which is safe to distribute.

## How trust works here

Signing proves *who* built the installer; it removes the warning only for
verifiers who trust the certificate's root. There are two kinds of root:

- **Self-managed PKI (what's set up now).** A root CA we generated ourselves.
  It removes the warning **only on machines that have installed our root CA**
  (run `install-root-ca.ps1`). Anyone downloading from the public Downloads
  page still sees "unknown publisher" and a SmartScreen prompt, because their
  Windows has never heard of our root. Cost: free.

- **CA-issued certificate (public trust).** A certificate from an authority
  Windows already trusts (Azure Trusted Signing, or an OV/EV cert). This one
  removes the warning for *everyone*, no root install needed. Cost: money +
  identity verification. Azure Trusted Signing (~$10/mo, no hardware token) is
  the cheapest route to full public trust; drop its cert into
  `WIN_CSC_LINK_B64` / `WIN_CSC_KEY_PASSWORD` and nothing else changes.

## Controlled machines (your PCs / known collaborators)

Ship them `whl-code-root-ca.crt` + `install-root-ca.ps1`, then once per machine:

```powershell
powershell -ExecutionPolicy Bypass -File install-root-ca.ps1
```

From then on, Library Tool installers install without the unknown-publisher
warning on that machine.

## How the build signs

`.github/workflows/release.yml` signs when the repo secret `WIN_CSC_LINK_B64`
(base64 of the signing `.pfx`) is present, using `WIN_CSC_KEY_PASSWORD`. With
no secret, the installer builds unsigned. Timestamping uses DigiCert's RFC-3161
server so signatures stay valid after the certificate expires.

Local signed build:

```powershell
$env:CSC_LINK = "C:\Users\amill\.whl-release\whl-code-signing.pfx"
$env:CSC_KEY_PASSWORD = "<pfx password from whl-codesign-info.txt>"
npm run dist
```

## Key material (not in the repo)

Lives in `C:\Users\amill\.whl-release\`, documented in `whl-codesign-info.txt`:
`whl-code-signing.pfx` (build signs with this), `whl-code-root-ca.pfx` (the CA
key — back up offline, used only to mint future signing certs), and the `.b64`
copy that feeds the GitHub secret.
