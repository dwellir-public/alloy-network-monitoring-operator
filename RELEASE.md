# Releasing to charmhub

Login to the user: dwellir-snapcrafters

```bash
charmcraft login
```

```bash
charmcraft pack
charmcraft upload alloy-vm_ubuntu@22.04-amd64.charm
charmcraft release alloy-vm -r <release-number> -c edge
charmcraft upload alloy-vm_ubuntu@24.04-amd64.charm
charmcraft release alloy-vm -r <release-number> -c edge
```
