"""Explicit worker identities; legacy identities retain their audit history."""
LEGACY_WORKERS = ("oliver-mac", "oliver-mini")
STUDIO_WORKERS = ("oliverhu-mac-studio", "oliverhu-mac-studio-vm1", "oliverhu-mac-studio-vm2")
MINI_VM_WORKERS = ("oliverhu-mini-vm1", "oliverhu-mini-vm2")
INITIALLY_PAUSED_WORKERS = MINI_VM_WORKERS
WORKERS = LEGACY_WORKERS + STUDIO_WORKERS + MINI_VM_WORKERS
