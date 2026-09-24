from pathlib import Path

# Update src/hilrig/models/__init__.py
models_init = Path("src/hilrig/models/__init__.py")
txt = models_init.read_text(encoding="utf-8")
if "from hilrig.models.protocol import ProtocolFamily" not in txt:
    txt = txt.replace(
        "from hilrig.models.identifiers import UploadAttempt",
        "from hilrig.models.identifiers import UploadAttempt\nfrom hilrig.models.protocol import ProtocolFamily",
    )
    txt = txt.replace(
        '"PointAssertion",',
        '"PointAssertion",\n    "ProtocolFamily",',
    )
    models_init.write_text(txt, encoding="utf-8")

# Update src/hilrig/results/adapter.py
adapter_file = Path("src/hilrig/results/adapter.py")
txt_a = adapter_file.read_text(encoding="utf-8")
txt_a = txt_a.replace(
    "from hilrig.protocol.application import ProtocolFamily",
    "from hilrig.models.protocol import ProtocolFamily",
)
adapter_file.write_text(txt_a, encoding="utf-8")

print("Imports fixed successfully")
