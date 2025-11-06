from setuptools import setup, find_packages

with open("requirements.txt") as f:
    requirements = f.read().splitlines()

setup(
    name="gmail-archiver",
    version="0.1.0",
    packages=find_packages(),
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "gmail-archiver=gmail_archiver.cli:main",
        ],
    },
    python_requires=">=3.8",
    author="Your Name",
    author_email="your.email@example.com",
    description="A tool to backup and restore Gmail emails with metadata",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/gmail-archiver",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
