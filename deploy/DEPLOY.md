# Hosting the Foundry dashboard on Windows (IIS)

Static files only: no Python, no database, no Foundry credentials on the server.

## One-time server setup
1. Install IIS and the **Windows Authentication** feature.
2. Create a folder, e.g. `C:\inetpub\foundry-dashboard`, and an IIS site (or
   application) pointing at it.
3. Edit `web.config` in the package: replace `DOMAIN\GROUP` with the AD group
   allowed to view this. Unedited, nobody can open it.

## Each update
1. On the Mac: `./deploy/package.sh` (add `--refresh` to pull new data first).
2. Copy the zip to the server and extract it over the site folder, replacing files.
   (`web.config` is included; if you edited the group on the server, keep your copy.)
3. Open the site; confirm the "generated" timestamp under the title is current.

## Notes
- The pages hold named-user activity and complaint text. Keep the AD group tight.
- `foundry_analytics.db` is never in the package. Do not copy it to the server.
- The Forge version (`dashboard-forge.html`) does not have the Users tab yet.
