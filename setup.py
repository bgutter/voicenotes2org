"""
voicenotes2org package config
"""

from setuptools import setup

setup(
    name='voicenotes2org',
    version='1.0.0',
    description='Transcribe voice recordings using the Google Cloud Speech-To-Text API, and export the results to Emacs org-mode headings.',
    long_description="See https://github.com/bgutter/voicenotes2org for all details!",
    url='https://github.com/bgutter/voicenotes2org',
    keywords='emacs org org-mode transcribe voice text',
    package_dir={'': 'src'},
    py_modules=["voicenotes2org"],
    python_requires='>=3.5',
    install_requires=['pydub','google-cloud-speech'],
    entry_points={
        'console_scripts': [
            'voicenotes2org = voicenotes2org:main',
        ],
    },
)
