"""Provisioning for a deployment: the device listener's certificate covers the real host name."""
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from urban.devices.registry import provision, server_names


def _signed_by_ca(pki):
    ca = x509.load_pem_x509_certificate((pki / "ca.pem").read_bytes())
    cert = x509.load_pem_x509_certificate((pki / "server.pem").read_bytes())
    ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
    return cert.issuer == ca.subject


def test_certificate_covers_the_host_and_is_reissued_under_the_same_ca(tmp_path):
    assert provision([], tmp_path, host="city.example") is True
    assert server_names(tmp_path) == {"localhost", "city.example"} and _signed_by_ca(tmp_path)
    ca = (tmp_path / "ca.pem").read_bytes()
    assert provision([], tmp_path, host="city.example") is False  # nothing to do
    assert provision([], tmp_path, host="new.example") is True    # a new name: reissued, same CA
    assert server_names(tmp_path) == {"localhost", "new.example"} and _signed_by_ca(tmp_path)
    assert (tmp_path / "ca.pem").read_bytes() == ca


def test_without_the_ca_key_the_certificate_is_left_alone(tmp_path, capsys):
    provision([], tmp_path, host="city.example")
    (tmp_path / "ca.key").unlink()
    before = (tmp_path / "server.pem").read_bytes()
    assert provision([], tmp_path, host="other.example") is False
    assert (tmp_path / "server.pem").read_bytes() == before and "ca.key" in capsys.readouterr().out
    assert provision([], tmp_path) is False  # no host asked for: the old demo layout keeps working
