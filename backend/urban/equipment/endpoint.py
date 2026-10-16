"""host, [host]:port, host:port or a bare IPv6 address -> (host, port)."""


def parse_endpoint(text: str, default_port: int) -> tuple[str, int]:
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        return host, int(rest[1:] or default_port) if rest.startswith(":") else default_port
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host, int(port or default_port)
    return text, default_port  # a bare name or a bare IPv6 address
