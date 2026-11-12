# The operator's SSH public keys, authorized for `jacob` (and root on the
# installer) on every system this repo installs: the USB installer, the
# scaffold a flashed host boots into, and the WSL bootstrap system. A first
# boot that fails is then debuggable over ssh with any fleet key, not only at
# the console; the dotfiles switch replaces this list with the deployed one.
{
  keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIASYCo8dKnRJ0Gc01yKMWRm4Afw7nXNASVtd5g8XV+vW jacob@mac"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDibOr+tge8OHe8sDZ+Hlhn83vN1P4Xcat1f4MJuYytM jacob@iphone"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILWh7JO+95XPqc41Up5gBCRkENckIj5/6zIIXZTobTzV jacob@pc1"
  ];
}
