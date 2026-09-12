# Prototype dependency notices

Phase 1 uses local, open-source developer dependencies only:

- Tauri: MIT/Apache-2.0
- React and React DOM: MIT
- Vite and TypeScript: MIT/Apache-2.0 project licensing
- lucide-react: ISC
- DuckDB Python package: MIT

Before release, audit all transitive runtime, build and installer dependencies
from the committed lockfiles and generated build artifacts.

