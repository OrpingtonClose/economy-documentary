# B2 checkpoint races

Knowledge page. Placeholder until slice 6 produces real incidents.

## Expected failure surface

- Two workers racing to upload the same artifact ID under different
  revision tags.
- A partial multipart upload leaving the bucket in an ambiguous state.
- A `load_manifest` that reads an artifact mid-write.

<!-- Fill in after slice 6 real runs -->
