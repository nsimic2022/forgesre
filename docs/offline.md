# Appliance without internet (air-gapped)

ForgeSRE runs without internet once its images are on the host. The bundle carries them.

## 1. Build the bundle (a machine with internet and Docker)

```bash
git clone https://github.com/nsimic2022/forgesre && cd forgesre
./forgesre bundle                    # or: ./forgesre bundle --include-models
```

This builds the Core image, pulls the image of every service of every Compose profile (ForgeAI,
worker, mailbox included) and writes `data/bundles/forgesre-bundle-<version>-<commit>.tar`:
the code (`git archive`: no `.env`, secrets, config or data), every image (`docker save`),
optionally the ForgeAI model files, the loader, a manifest and SHA-256 sums. Expect several GB.

## 2. Load it on the appliance

Copy the file over (USB disk, jump host), then:

```bash
tar -xf forgesre-bundle-<version>-<commit>.tar load-bundle.sh
./load-bundle.sh forgesre-bundle-<version>-<commit>.tar      # unpacks into ./forgesre
cd forgesre && ./install.sh --offline                         # new appliance
```

The loader checks every file against the sums (a damaged bundle is refused), loads the images,
unpacks the code and marks the host as offline, so install and update start the loaded Core image
instead of building it (a build would need pip and the internet).

## 3. Update later

Build a new bundle the same way, copy it, and on the appliance:

```bash
./scripts/load-bundle.sh /path/to/forgesre-bundle-<new>.tar   # code is unpacked over this checkout
./forgesre update --offline
```

`.env`, `secrets/`, `config/forgesre.yml` and `data/` are not in the bundle and are kept. The
usual pre-update backup runs as always.

## What still needs a network

Nothing outside your own network: alarms, mail through your SMTP relay, chat webhooks to an
on-premises server, GLPI, Zabbix, LibreNMS and NetBox inside the network, the local ForgeAI model.
Model downloads from Hugging Face in Settings › ForgeAI do not work offline; use
`--include-models` or **Import** a GGUF file there.
