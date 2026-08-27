# DiscordPBX v3.3.11

Contact-sharing and bulk-number import release.

## Contacts

- Any authenticated operator with the workspace `contacts` capability can create a new Global contact.
- CSV import can create new Global contacts for Contacts-capable operators.
- Existing Global contacts remain protected: non-system administrators cannot edit/delete them or use a CSV number collision to overwrite/promote them.
- The Contacts UI keeps the Global scope selector enabled for permitted operators and hides Global Edit/Delete controls when the server would reject them.

## Bulk phone-number imports

- Raise the bulk input ceiling from 1.5 MB to **25 MB**.
- Raise the parser's phone-entry ceiling from 25,000 to **30,000,000** entries.
- Add TXT/CSV file pickers to Caller ID, Random Destination, and number-block bulk inputs; files larger than 25 MB are rejected client-side before upload.
- Keep the server-side 25 MB check authoritative and measure UTF-8 bytes rather than Python character count.
- Existing NPA-NXX block pools remain the recommended representation for truly massive ranges because each six-digit block represents 10,000 numbers without materializing every number.

## Releases

- The release workflow now keeps the current release as the only visible GitHub Release entry after successful publication, while historical Git tags remain intact.
