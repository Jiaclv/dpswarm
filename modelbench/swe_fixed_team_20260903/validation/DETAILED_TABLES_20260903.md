### 表1 逐 run 总览(两批 14 个 run)

| 批次 | 题目 | arm | 官方 | 调用 | Lead token | Worker token | 合计 token | 未知用量 | 墙钟s | 结局 | worker采纳 |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|
| v2 | Matplotlib | glm-5.3 | 通过 | 26 | 255,850 | 164,728 | 420,578 | 0 | 425 | completed | 0/2 |
| v2 | Matplotlib | glm-5.3-flash | 通过 | 27 | 299,648 | 184,523 | 484,171 | 0 | 691 | completed | 0/2 |
| v2 | Matplotlib | gpt-5.6-luna | 通过 | 23 | 206,182 | 331,775 | 537,957 | 0 | 392 | completed | 2/2 |
| v2 | Matplotlib | gpt-5.6-sol | 通过 | 19 | 232,884 | 214,409 | 447,293 | 0 | 479 | completed | 2/2 |
| v2 | Matplotlib | gpt-5.6-terra | 通过 | 15 | 119,048 | 212,619 | 331,667 | 0 | 237 | completed | 2/2 |
| v2 | Matplotlib | solo | 通过 | 7 | 177,552 | 0 | 177,552 | 0 | 233 | completed | — |
| v2 | Sphinx | gpt-5.6-luna | 通过 | 23 | 147,051 | 410,152 | 557,203 | 0 | 356 | budget_exhausted | 1/2 |
| v2 | Sphinx | gpt-5.6-terra | 未过 | 22 | 216,228 | 191,599 | 407,827 +1未知 | 1 | 426 | budget_exhausted | 1/2 |
| v3 | Sphinx | glm-5.3 | 通过 | 28 | 243,757 | 165,467 | 409,224 | 0 | 369 | completed | 0/2 |
| v3 | Sphinx | glm-5.3-flash | 通过 | 25 | 353,051 | 134,872 | 487,923 +1未知 | 1 | 1140 | budget_exhausted | 0/2 |
| v3 | Sphinx | gpt-5.6-luna | 未过 | 24 | 188,889 | 387,546 | 576,435 | 0 | 416 | budget_exhausted | 2/2 |
| v3 | Sphinx | gpt-5.6-sol | 通过 | 25 | 200,559 | 360,838 | 561,397 | 0 | 500 | completed | 2/2 |
| v3 | Sphinx | gpt-5.6-terra | 未过 | 23 | 166,729 | 332,902 | 499,631 | 0 | 311 | completed | 2/2 |
| v3 | Sphinx | solo | 未过 | 15 | 512,199 | 0 | 512,199 | 0 | 370 | budget_exhausted | — |

### 表2 逐 agent 明细(38 个实例化 agent;cache 含于 input,reasoning 含于 output)

| 批次 | run/arm | 角色 | 模型 | 调用 | input(含cache) | cache | output(含reasoning) | reasoning | total | 调用秒 | agent墙钟s | 终态 |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| v2 | M:solo | lead | gpt-5.6-sol | 7 | 170,852 | 75,136 | 6,700 | null | 177,552 | 112 | 144 | finished |
| v2 | M:glm-5.3 | lead | gpt-5.6-sol | 10 | 247,630 | 111,360 | 8,220 | null | 255,850 | 144 | 387 | finished |
| v2 | M:glm-5.3 | worker | glm-5.3 | 8 | 51,507 | 40,448 | 6,148 | 3,867 | 57,655 | 143 | 156 | failed |
| v2 | M:glm-5.3 | worker | glm-5.3 | 8 | 98,656 | 77,312 | 8,417 | 6,972 | 107,073 | 210 | 220 | failed |
| v2 | M:glm-5.3-flash | lead | gpt-5.6-sol | 11 | 292,216 | 109,696 | 7,432 | null | 299,648 | 139 | 654 | finished |
| v2 | M:glm-5.3-flash | worker | glm-5.3-flash | 8 | 34,644 | 26,560 | 4,888 | 3,404 | 39,532 | 187 | 200 | failed |
| v2 | M:glm-5.3-flash | worker | glm-5.3-flash | 8 | 126,787 | 100,864 | 18,204 | 15,976 | 144,991 | 535 | 555 | failed |
| v2 | M:gpt-5.6-sol | lead | gpt-5.6-sol | 9 | 228,659 | 95,872 | 4,225 | null | 232,884 | 90 | 442 | finished |
| v2 | M:gpt-5.6-sol | worker | gpt-5.6-sol | 4 | 64,984 | 41,472 | 1,545 | null | 66,529 | 33 | 42 | adopted |
| v2 | M:gpt-5.6-sol | worker | gpt-5.6-sol | 6 | 142,946 | 62,208 | 4,934 | null | 147,880 | 87 | 93 | adopted |
| v2 | M:gpt-5.6-terra | lead | gpt-5.6-sol | 5 | 117,350 | 51,840 | 1,698 | null | 119,048 | 42 | 170 | finished |
| v2 | M:gpt-5.6-terra | worker | gpt-5.6-terra | 5 | 84,841 | 32,000 | 3,293 | null | 88,134 | 58 | 68 | adopted |
| v2 | M:gpt-5.6-terra | worker | gpt-5.6-terra | 5 | 118,341 | 48,128 | 6,144 | null | 124,485 | 92 | 99 | adopted |
| v2 | M:gpt-5.6-luna | lead | gpt-5.6-sol | 10 | 203,330 | 106,240 | 2,852 | null | 206,182 | 78 | 355 | finished |
| v2 | M:gpt-5.6-luna | worker | gpt-5.6-luna | 5 | 81,092 | 33,024 | 3,734 | null | 84,826 | 67 | 77 | adopted |
| v2 | M:gpt-5.6-luna | worker | gpt-5.6-luna | 8 | 237,483 | 132,608 | 9,466 | null | 246,949 | 160 | 166 | adopted |
| v2 | S:gpt-5.6-luna | lead | gpt-5.6-sol | 7 | 144,131 | 72,576 | 2,920 | null | 147,051 | 104 | 289 | finished |
| v2 | S:gpt-5.6-luna | worker | gpt-5.6-luna | 8 | 221,096 | 90,112 | 16,806 | null | 237,902 | 241 | 245 | adopted |
| v2 | S:gpt-5.6-luna | worker | gpt-5.6-luna | 8 | 166,057 | 126,976 | 6,193 | null | 172,250 | 119 | 120 | failed |
| v2 | S:gpt-5.6-terra | lead | gpt-5.6-sol | 8 | 213,693 | 72,576 | 2,535 | null | 216,228 | 66 | 404 | finished |
| v2 | S:gpt-5.6-terra | worker | gpt-5.6-terra | 6 | null | null | null | null | null | 142 | 145 | failed |
| v2 | S:gpt-5.6-terra | worker | gpt-5.6-terra | 8 | 178,329 | 92,160 | 13,270 | null | 191,599 | 194 | 199 | adopted |
| v3 | S:gpt-5.6-luna | lead | gpt-5.6-sol | 8 | 185,770 | 47,488 | 3,119 | null | 188,889 | 86 | 394 | finished |
| v3 | S:gpt-5.6-luna | worker | gpt-5.6-luna | 8 | 214,697 | 114,432 | 12,929 | null | 227,626 | 193 | 197 | adopted |
| v3 | S:gpt-5.6-luna | worker | gpt-5.6-luna | 8 | 151,055 | 54,784 | 8,865 | null | 159,920 | 141 | 145 | adopted |
| v3 | S:gpt-5.6-terra | lead | gpt-5.6-sol | 8 | 164,216 | 0 | 2,513 | null | 166,729 | 67 | 189 | finished |
| v3 | S:gpt-5.6-terra | worker | gpt-5.6-terra | 7 | 136,202 | 72,192 | 8,334 | null | 144,536 | 129 | 132 | adopted |
| v3 | S:gpt-5.6-terra | worker | gpt-5.6-terra | 8 | 177,951 | 84,224 | 10,415 | null | 188,366 | 155 | 159 | adopted |
| v3 | S:gpt-5.6-sol | lead | gpt-5.6-sol | 9 | 196,762 | 86,912 | 3,797 | null | 200,559 | 111 | 264 | finished |
| v3 | S:gpt-5.6-sol | worker | gpt-5.6-sol | 8 | 173,084 | 62,208 | 13,098 | null | 186,182 | 198 | 206 | adopted |
| v3 | S:gpt-5.6-sol | worker | gpt-5.6-sol | 8 | 169,431 | 61,824 | 5,225 | null | 174,656 | 144 | 148 | adopted |
| v3 | S:glm-5.3-flash | lead | gpt-5.6-sol | 13 | 344,075 | 132,352 | 8,976 | null | 353,051 | 230 | 1119 | finished |
| v3 | S:glm-5.3-flash | worker | glm-5.3-flash | 4 | null | null | null | null | null | 318 | 320 | failed |
| v3 | S:glm-5.3-flash | worker | glm-5.3-flash | 8 | 105,919 | 73,088 | 28,953 | 25,943 | 134,872 | 894 | 901 | failed |
| v3 | S:glm-5.3 | lead | gpt-5.6-sol | 10 | 235,560 | 82,944 | 8,197 | null | 243,757 | 175 | 347 | finished |
| v3 | S:glm-5.3 | worker | glm-5.3 | 9 | 82,793 | 49,728 | 7,363 | 6,040 | 90,156 | 151 | 156 | failed |
| v3 | S:glm-5.3 | worker | glm-5.3 | 9 | 63,380 | 37,120 | 11,931 | 9,509 | 75,311 | 204 | 210 | failed |
| v3 | S:solo | lead | gpt-5.6-sol | 15 | 493,037 | 145,536 | 19,162 | null | 512,199 | 326 | 348 | finished |

### 表3 按 模型×角色 聚合(两批合计)

| 模型 | 角色 | 调用 | input | cache | output | reasoning | total | 调用秒 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| glm-5.3 | worker | 34 | 296,336 | 204,608 | 33,859 | 26,388 | 330,195 | 708 |
| glm-5.3-flash | worker | 28 | 267,350 | 200,512 | 52,045 | 45,323 | 319,395 +未知×1 | 1935 |
| gpt-5.6-luna | worker | 45 | 1,071,480 | 551,936 | 57,993 | 0 | 1,129,473 | 920 |
| gpt-5.6-sol | lead | 130 | 3,237,281 | 1,190,528 | 82,346 | 0 | 3,319,627 | 1769 |
| gpt-5.6-sol | worker | 26 | 550,445 | 227,712 | 24,802 | 0 | 575,247 | 461 |
| gpt-5.6-terra | worker | 39 | 695,664 | 328,704 | 41,456 | 0 | 737,120 +未知×1 | 771 |
