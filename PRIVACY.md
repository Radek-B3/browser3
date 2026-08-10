# Privacy

Browser3 does not add a vendor telemetry or browsing-activity reporting service. The maintainer does not receive your browsing history, cookies, generated identities, proxy credentials, or hardware-probe results through Browser3.

## Data stored on your computer

Mutable Browser3 data is stored under `%LOCALAPPDATA%\Browser3`:

- generated identities and Chromium user data in `profiles`;
- hardware, codec, and proxy-geolocation cache in `cache`;
- sticky mappings and migration records in `state`;
- local diagnostic logs in `logs`.

Chromium browsing data remains in the selected profile's user-data directory. Removing the Browser3 LocalAppData directory removes Browser3-managed local state, but it is destructive and cannot be undone; close Browser3 and back up anything needed first.

## Network requests added by Browser3

When a proxy is configured, Browser3 requests the proxy exit's country and time zone from the free `ip-api.com` endpoint through that proxy. The endpoint sees the proxy exit address, and the response is cached locally. The free endpoint uses unencrypted HTTP. Browser3 makes no such request when no proxy is configured.

Chromium itself uses network services such as Safe Browsing, component updates, search suggestions, and certificate checks. Browser3 does not claim to disable every Chromium service. Review Chromium settings and policies for the controls available to you.

## Logs and support

Local logs are not uploaded automatically. If you choose to attach logs to a support request, remove proxy credentials, public IP addresses, generated profile identifiers, visited URLs, and personal data first.
