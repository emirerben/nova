require "base64"
require "json"
require "net/http"
require "openssl"

def request(uri, method, label, payload: nil, bearer: nil)
  req = method.new(uri)
  req["Authorization"] = "Bearer #{bearer}" if bearer
  if payload
    req["Content-Type"] = "application/json"
    req.body = JSON.generate(payload)
  end
  response = Net::HTTP.start(uri.host, uri.port, use_ssl: true, open_timeout: 15, read_timeout: 30) { |http| http.request(req) }
  abort "#{label} failed (HTTP #{response.code})" unless response.is_a?(Net::HTTPSuccess)
  JSON.parse(response.body)
rescue JSON::ParserError
  abort "#{label} returned invalid JSON"
end

def encode(value)
  Base64.urlsafe_encode64(value, padding: false)
end

def apple_token
  key = OpenSSL::PKey.read(Base64.decode64(ENV.fetch("ASC_KEY")))
  header = { alg: "ES256", kid: ENV.fetch("ASC_KEY_ID"), typ: "JWT" }
  claims = { iss: ENV.fetch("ASC_ISSUER"), aud: "appstoreconnect-v1", exp: Time.now.to_i + 600 }
  unsigned = [header, claims].map { |part| encode(JSON.generate(part)) }.join(".")
  signature = OpenSSL::ASN1.decode(key.sign("SHA256", unsigned)).value.map { |n| n.value.to_s(2).rjust(32, "\0") }.join
  "#{unsigned}.#{encode(signature)}"
end

email = ENV.fetch("REVIEW_EMAIL")
password = ENV.fetch("REVIEW_PASSWORD")
abort "Missing demo credentials" if email.empty? || password.empty?
login = request(URI("https://nova-video.fly.dev/auth/mobile/reviewer-login"), Net::HTTP::Post, "Kria demo login",
                payload: { email: email, password: password })
abort "Kria did not confirm the demo session" if login["access_token"].to_s.empty?
puts "Production demo login verified."

apple = "https://api.appstoreconnect.apple.com/v1"
token = apple_token
versions = URI("#{apple}/apps/6811999144/appStoreVersions")
versions.query = URI.encode_www_form("filter[platform]" => "IOS", "filter[versionString]" => "1.0")
rows = request(versions, Net::HTTP::Get, "Kria version lookup", bearer: token).fetch("data")
abort "Expected one Kria 1.0 version" unless rows.size == 1
version = rows.first
editable = %w[PREPARE_FOR_SUBMISSION DEVELOPER_REJECTED METADATA_REJECTED REJECTED]
abort "Version is outside the editable submission states" unless editable.include?(version.dig("attributes", "appStoreState"))
detail = URI("#{apple}/appStoreVersions/#{version.fetch('id')}/appStoreReviewDetail")
id = request(detail, Net::HTTP::Get, "Review detail lookup", bearer: token).fetch("data").fetch("id")
attributes = { demoAccountRequired: true, demoAccountName: email, demoAccountPassword: password }
payload = { data: { type: "appStoreReviewDetails", id: id, attributes: attributes } }
saved = request(URI("#{apple}/appStoreReviewDetails/#{id}"), Net::HTTP::Patch, "Review login update", bearer: token, payload: payload)
abort "Unexpected review detail in Apple response" unless saved.dig("data", "id") == id
puts "Apple accepted the verified demo account for Kria 1.0."
