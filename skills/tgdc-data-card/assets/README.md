# Renderer assets

`tgdc-logo-horizontal.png` is the current horizontal logo of The General Data Company, unchanged, with the helmet and the complete two-line company name. It is a TGDC brand asset; the Apache License 2.0 of this repository does not grant rights to use it as a trademark. Its Inter Medium lettering is already part of the image. The renderer preserves its 3200:1200 proportions and applies a black transparency mask only while embedding it in the PDF. It does not modify the image file. No historical TGDC wordmark, recipient or customer logos are included.

The unchanged static font files are:

- Outfit Light/Regular/Medium, version 1.100. Copyright 2021 The Outfit Project Authors. Official license: https://github.com/google/fonts/blob/main/ofl/outfit/OFL.txt
- JetBrains Mono Regular, version 2.211. Copyright 2020 The JetBrains Mono Project Authors. Official license: https://github.com/JetBrains/JetBrainsMono/blob/master/OFL.txt

Each font family includes its complete original SIL Open Font License 1.1 in `fonts/Outfit/OFL.txt` or `fonts/JetBrainsMono/OFL.txt`. Both fonts include the tested German and Polish accented characters. Report text outside the font character set is rejected before replacing the output, with a request for an English or Latin-script summary. Scanner language classification is independent of this report-font restriction. Control characters are displayed as explicit Unicode code-point labels instead of being silently lost.
