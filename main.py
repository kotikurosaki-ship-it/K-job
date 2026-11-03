[app]
title = JobBot
package.name = jobbot
package.domain = org.yourname
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json,ttf
version = 2.0

requirements = python3,kivy==2.3.0,openssl,pyjnius,android,plyer,androidstorage4kivy,certifi,fpdf2,pypdf

orientation = portrait
osx.python_version = 3
fullscreen = 0

android.permissions = INTERNET, ACCESS_NETWORK_STATE
android.api = 34
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = False
android.enable_androidx = True

[buildozer]
log_level = 2
warn_on_root = 1
