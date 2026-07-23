"""Assinatura das requisições do QZ Tray.

O QZ Tray só oferece "lembrar desta decisão" para requisições ASSINADAS. Assinamos
o desafio (toSign) com esta chave privada (SHA512/RSA) e apresentamos o certificado
público. Chave de baixo risco: só autoriza impressões no QZ local (não protege nada
sensível). Para ZERO prompts, o certificado pode ser adicionado à lista confiável do
QZ Tray; sem isso, o usuário clica "Permitir + lembrar" uma única vez por certificado.
"""
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

QZ_CERT = """-----BEGIN CERTIFICATE-----
MIIDMTCCAhmgAwIBAgIUIMV4JJ1RBTKlpv1ZaDd3ex7EbiswDQYJKoZIhvcNAQEL
BQAwKDEOMAwGA1UEAwwFRVJQZXgxFjAUBgNVBAoMDU1lcmNhZG9TZWxsZXIwHhcN
MjYwNzA4MTQ1MjUzWhcNMzYwNzA1MTQ1MjUzWjAoMQ4wDAYDVQQDDAVFUlBleDEW
MBQGA1UECgwNTWVyY2Fkb1NlbGxlcjCCASIwDQYJKoZIhvcNAQEBBQADggEPADCC
AQoCggEBAJnTGbTtxyfpt26HwTXr/WbUVynQLPh6JWFwuX1sSoGdQbIa1DzhFCC8
xzn+3NulXAkoLAkr1hr1OsXRan7VVxLd/kvg6LAzgk1Grz3c5W6Ai2odmVSHnVub
F7WD+2iz74joQGsGA0UgRNUtRpuFMQ9joBo4N7Hch+oCazPySInZlrs7//KtqzV2
MxNZlNeDojFoJsMAVpKnkR4Iv9TviXGV3iH8Ik6Jd5QcW8WYqMKbwYtb7V2dDP7o
3oAtkSeG7nZc8PX+68a3nZkqgKFgFpOkCB9bdLCdmY0oZPqxAgP5ahy0K7+76dQW
gywhyTi+1PanpxifSAWAOoNSTWiL+FkCAwEAAaNTMFEwHQYDVR0OBBYEFNBFgWUX
Dx8U4WvnfZkdSbSsoWO/MB8GA1UdIwQYMBaAFNBFgWUXDx8U4WvnfZkdSbSsoWO/
MA8GA1UdEwEB/wQFMAMBAf8wDQYJKoZIhvcNAQELBQADggEBACAvMzhT30nP6yYd
UKStOSg+TRIPNSUBGOZV92bLAVIX2ypi1m0XUDb7ZhMS89UORHK0fK9f2+cVoKre
lsnAU9FHWcmIw+6Y2Mx3o3VJJ1zdSFwXbaiyu6hlyqMYfVGLRw9JSpTf2zqAssP3
j8zXEN47d7dQzGj/8Adbj7i1UB58sH5kXLZkWNrO7IaAbKu18BM/QKXlx3qmWWZi
XKLh+tnZcc0wV0k7F/EurBPGHc4cPa42wBRpTqt9+uUldS3XsupAObP5NNKFcGyB
bUyKmK/YCNQTteYn6aoeVfac7a+sGIAoHrpPtBIYfWnQz5o9mE/7oDT82C4mvEEo
AGArcwI=
-----END CERTIFICATE-----"""

_QZ_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCZ0xm07ccn6bdu
h8E16/1m1Fcp0Cz4eiVhcLl9bEqBnUGyGtQ84RQgvMc5/tzbpVwJKCwJK9Ya9TrF
0Wp+1VcS3f5L4OiwM4JNRq893OVugItqHZlUh51bmxe1g/tos++I6EBrBgNFIETV
LUabhTEPY6AaODex3IfqAmsz8kiJ2Za7O//yras1djMTWZTXg6IxaCbDAFaSp5Ee
CL/U74lxld4h/CJOiXeUHFvFmKjCm8GLW+1dnQz+6N6ALZEnhu52XPD1/uvGt52Z
KoChYBaTpAgfW3SwnZmNKGT6sQID+WoctCu/u+nUFoMsIck4vtT2p6cYn0gFgDqD
Uk1oi/hZAgMBAAECggEABF8KvRjgNtbGDQyrzYrZIr/QQcqOCpoa/5W+2az3AY+s
1yE6zIeXZTZEIZZn45CDiOmUtcaCgTXcj5RLxtKsdKdtE24u4pn2HxjOAE+rj+w0
egWhDrVDYB/sSK7ZWUB6wzzZXsk3FNLtzdKJbgA3FJE/0prsH0BtdtWOgxWEoPEs
DoIoS/Kx8bl11WUgg9kWkLZ0CWswvBbBptUB7pb1edKuXRBB6NZkafewazymmCJU
ws0A1M7ImMwVSKg/JTsf3TYL0Ce7dFL3swKTKHB7R3A4JK/k808FLkABLx02UMNH
QsS2Ymkji9isfsyMnM0K7MDQOoc2ftXfxRgjGmNPYQKBgQDWtLPFE7zPknsVO2AR
PjEDuX4NdUaqA2rPandcwdB7JB5+hsHo2CHKT8df/vJEMxgXU8IdYAQVtnsnACcn
sB156gGf6k5oeLSV6ZZMovX2aVEAqyFazkb49zbDn4HScCjpCAtLh8P0Qaw41tv1
TiDR7Q458VuenVhkwbCQgHpUeQKBgQC3aNZeJiDlwD0JjdItxsXoS0Wp6Sj6PKj9
WK+KRX9wnZ4cecG6b/0lX6wP6ByUQQkZsgXwqb++El2FFp/PEZ7ytOKSoy4eOvei
iU98I/2JlxJcVJ+YeRm3n93yyMU/xEF9/6Y515KFQLv/P/pKA6XVSrAO3HXZIgAy
rtRzRtMK4QKBgHG5KxMzHiowI0OevIbFkz6uzKaiPLimsLeGZAzcl+nxurk39ZO4
j0VStn8RUg9vpM4OTl4y0lcR3e9NdG/gJ+zAVvX2LGvHq5dQL40OMAvBwucAvd0U
L6GFiBtb7G6je/fai+kI03EYK/m7TKyFInsu/f8Q6X99RimwMi6H7sO5AoGAG/O6
V/bvpJQ7uS0IDznwB4sRPuft+tUr3BCcEDKvTXZ4FlboE4XlysBd9L6nPGD9BhF/
nkIAmvMplZLxIBnLY6n1ret4p9rMytSqbHz/sux3O+MZv58VMEsJBGtcxG8gnBdO
OhqElhJblHcnqggMSglr85fdzg3EgfTrI/ZzS4ECgYBQVvH/n+oqHpWxAK9BSsCU
vOaqvuaXBzmsXtSAJWLSyskf19o+43PLRtMSakZ003wq3v4bEI2mxRtl2cXPQlzg
zWjXh39b78Y7ZdXo+zxawo6jSlOxIiva97QOmckWSL67Tf90hpHLCUd+Lyd5e02B
BH027fQojbjtsmldfPK7SQ==
-----END PRIVATE KEY-----"""

_priv = serialization.load_pem_private_key(_QZ_PRIVATE_KEY.encode(), password=None)


def sign(data: str) -> str:
    """Assina o desafio do QZ (RSA/SHA512, PKCS1v15) e devolve em base64."""
    sig = _priv.sign(data.encode("utf-8"), padding.PKCS1v15(), hashes.SHA512())
    return base64.b64encode(sig).decode()
