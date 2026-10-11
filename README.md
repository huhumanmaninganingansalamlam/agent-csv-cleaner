# Agent-to-Agent API: CSV Cleaner

A local FastAPI wrapper around the original CSV Cleanup Kit core. Upload CSV; receive cleaned JSON and an audit. Simulated metering is exactly USD 0.001 per successful unique request. No actual payment or revenue collection is implemented.

See [README_A2A.md](README_A2A.md) for Docker, Compose, curl, options, retry accounting and deployment limits.

## Verification

- 69 tests passed locally on Linux / Python 3.12.14 (28 API tests + 41 unchanged core regression tests).
- Live uvicorn + curl smoke test passed: cleaned JSON, USD 0.001 simulated entry, idempotent retry at zero additional charge, and usage totals.
- The existing csv_cleanup.py is reused unchanged; SHA-256: 318c00cbd6181d1081c6768d62febe50bd5e1dd6617184593259672636d054c0.
- No local Docker engine was available. Docker image build/run is not yet verified locally. The GitHub Actions workflow builds and starts the image, runs real HTTP assertions, and uploads the image only after success. Check the actual workflow result before claiming a verified image.
- Source publication does not mean this API is deployed or taking payments. Seller terms and software licensing have not been selected.

All bundled sample data is synthetic. No secrets, customer files or original project-private code are included.
