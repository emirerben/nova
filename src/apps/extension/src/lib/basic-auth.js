// UTF-8-safe BasicAuth header builder for the Kria Music Ingest extension.
//
// btoa() is strictly Latin-1: passing a string with non-ASCII characters
// throws `InvalidCharacterError`. For BasicAuth we MUST send UTF-8 bytes
// in the base64 payload (RFC 7617 §2.1 specifies UTF-8 by default), so
// the right idiom is: UTF-8-encode the credentials to bytes, base64 those
// bytes. The middleware on the server side decodes the same way.
//
// Without this helper, an admin with a password like `pässwørd` would see
// the popup's "Test connection" throw silently inside `btoa`, the offscreen
// doc's fetch go out with no Authorization header, and the middleware
// reject with a confusing 401.

export function encodeBasicAuthHeader(user, pass) {
  const bytes = new TextEncoder().encode(`${user}:${pass}`);
  // Convert byte array to a binary string (each byte → one char code) so
  // btoa accepts it. This is the canonical "btoa(utf-8)" workaround.
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return "Basic " + btoa(bin);
}
