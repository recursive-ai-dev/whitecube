#!/bin/bash
# Run with sudo: rewrites the visible MX branding in root-owned files. Backups go to /root/mx-rebrand-bak.
set -e
B=/root/mx-rebrand-bak; mkdir -p $B
cp -p /etc/lsb-release /etc/mx-version $B/
cat > /etc/lsb-release <<L
PRETTY_NAME="WhiteCube"
DISTRIB_ID=WhiteCube
DISTRIB_RELEASE=1
DISTRIB_CODENAME=whitecube
DISTRIB_DESCRIPTION="WhiteCube"
L
echo "WhiteCube" > /etc/mx-version
for f in /usr/share/plymouth/themes/*/*.plymouth; do
  grep -q -E '^(Name|Description)=.*MX' "$f" || continue
  cp -p "$f" "$B/$(basename "$(dirname "$f")")-$(basename "$f")"
  sed -i -E '/^(Name|Description)=/{s/MX[ -]Linux/WhiteCube/g; s/\bMX\b/WhiteCube/g}' "$f"
done
for f in /usr/share/applications/mx-checkaptgpg.desktop /usr/share/applications/verify-iso-sig.desktop; do
  [ -f "$f" ] || continue
  cp -p "$f" "$B/"
  sed -i -E '/^(Name|Comment|GenericName)\[[^]]*\]=/d; /^(Name|Comment|GenericName)=/{s/MX[ -]Linux/WhiteCube/g; s/\bMX\b/WhiteCube/g}' "$f"
done
echo done
