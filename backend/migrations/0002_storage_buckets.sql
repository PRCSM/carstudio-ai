-- Private buckets; objects are only reachable via service credentials or short-lived signed URLs.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
  ('car-originals', 'car-originals', false, 15728640, array['image/jpeg','image/png','image/webp','image/heic']),
  ('car-results',   'car-results',   false, 15728640, array['image/jpeg','image/png','image/webp'])
on conflict (id) do nothing;
