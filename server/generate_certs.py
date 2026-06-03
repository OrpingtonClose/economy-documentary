import os
import secrets
from pathlib import Path
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

def generate_key_and_cert(
    common_name: str,
    is_ca: bool = False,
    signing_key = None,
    signing_cert = None,
    sans: list[str] = None,
    is_client: bool = False
):
    # Generate RSA private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Prepare subject/issuer names
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Economy Documentary"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])

    if is_ca or signing_cert is None:
        issuer = subject
        cert_signing_key = private_key
    else:
        issuer = signing_cert.subject
        cert_signing_key = signing_key

    # Validity range: 1 day (short-lived dynamic certificates)
    not_valid_before = datetime.now(timezone.utc)
    not_valid_after = not_valid_before + timedelta(days=1)

    builder = x509.CertificateBuilder()
    builder = builder.subject_name(subject)
    builder = builder.issuer_name(issuer)
    builder = builder.public_key(private_key.public_key())
    builder = builder.serial_number(x509.random_serial_number())
    builder = builder.not_valid_before(not_valid_before)
    builder = builder.not_valid_after(not_valid_after)

    # Basic constraints
    builder = builder.add_extension(
        x509.BasicConstraints(ca=is_ca, path_length=None),
        critical=True
    )

    # Subject Key Identifier
    builder = builder.add_extension(
        x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
        critical=False
    )

    # Authority Key Identifier
    if signing_cert is not None:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signing_cert.public_key()),
            critical=False
        )
    else:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(private_key.public_key()),
            critical=False
        )

    # Key Usage and Extended Key Usage for strict OpenSSL validation
    if is_ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False
            ),
            critical=True
        )
    else:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False
            ),
            critical=True
        )
        if is_client:
            eku = x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH])
        else:
            eku = x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH])
        builder = builder.add_extension(eku, critical=True)

    # Subject Alternative Names (SAN) for server hostname/IP verification
    if sans:
        san_list = []
        for san in sans:
            if san.replace('.', '').isdigit(): # Simple check for IP vs Domain
                san_list.append(x509.IPAddress(os.sys.modules['ipaddress'].ip_address(san)))
            else:
                san_list.append(x509.DNSName(san))
        builder = builder.add_extension(
            x509.SubjectAlternativeName(san_list),
            critical=False
        )

    # Sign certificate
    cert = builder.sign(
        private_key=cert_signing_key,
        algorithm=hashes.SHA256(),
    )

    # Serialize private key to PEM
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    )

    # Serialize certificate to PEM
    cert_pem = cert.public_bytes(
        encoding=serialization.Encoding.PEM
    )

    return private_key, cert, key_pem, cert_pem

def main():
    # Import ipaddress dynamically to support IP SAN checks
    import ipaddress
    
    pki_dir = Path("/tmp/documentary-pipeline/pki")
    pki_dir.mkdir(parents=True, exist_ok=True)

    print("Generating zero-trust PKI certificates...")

    # 1. Generate Root CA
    ca_key, ca_cert, ca_key_pem, ca_cert_pem = generate_key_and_cert(
        common_name="Economy Documentary Root CA",
        is_ca=True
    )
    (pki_dir / "ca.key").write_bytes(ca_key_pem)
    (pki_dir / "ca.crt").write_bytes(ca_cert_pem)

    # 2. Generate Server Certificate (covering localhost, 127.0.0.1)
    _, _, server_key_pem, server_cert_pem = generate_key_and_cert(
        common_name="localhost",
        is_ca=False,
        signing_key=ca_key,
        signing_cert=ca_cert,
        sans=["localhost", "127.0.0.1"]
    )
    (pki_dir / "server.key").write_bytes(server_key_pem)
    (pki_dir / "server.crt").write_bytes(server_cert_pem)

    # 3. Generate Client Certificate
    _, _, client_key_pem, client_cert_pem = generate_key_and_cert(
        common_name="internal-agent-client",
        is_ca=False,
        signing_key=ca_key,
        signing_cert=ca_cert,
        is_client=True
    )
    (pki_dir / "client.key").write_bytes(client_key_pem)
    (pki_dir / "client.crt").write_bytes(client_cert_pem)

    # 4. Generate dynamic RUN_SECRET token
    run_secret = secrets.token_hex(16)
    secret_file = Path("/tmp/documentary-pipeline/run_secret.txt")
    secret_file.write_text(run_secret, encoding="utf-8")

    print(f"PKI generation complete. Certificates written to {pki_dir}")

if __name__ == "__main__":
    main()
