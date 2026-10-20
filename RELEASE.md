# Releasing to Charmhub

Use the account authorized to publish `alloy-nm`. Complete the
[build and acceptance checks](docs/build-test-deploy.md) before publishing.

```bash
charmcraft login
```

```bash
charmcraft pack --platform ubuntu@22.04:amd64
charmcraft pack --platform ubuntu@24.04:amd64
charmcraft upload alloy-nm_ubuntu@22.04-amd64.charm
charmcraft upload alloy-nm_ubuntu@24.04-amd64.charm
```

Use the revision returned for each base when releasing to the intended channel:

```bash
charmcraft release alloy-nm -r <22.04-revision> -c edge
charmcraft release alloy-nm -r <24.04-revision> -c edge
```
