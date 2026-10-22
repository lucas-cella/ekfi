# ekfi
Smart monitor for WLAN/Wi-Fi Site

**Ekfi by Ektech** é um monitor interativo de terminal (no estilo do `htop`) que mostra todos os KPIs do Wi-Fi ao qual você está conectado, atualizados em tempo real.

## Requisitos

- Linux com NetworkManager
- Python 3.12+ (só biblioteca padrão, sem dependências)
- `iw`, `nmcli` e `ping` (já vêm na maioria das distribuições)
- Opcionais: `ethtool` (contadores do driver), `dig` (tempo de DNS), `lspci` (chipset), `journalctl` (histórico de conexões)

Não precisa de root.

## Uso

```bash
./ekfi.py                 # monitor interativo
./ekfi.py --once          # relatório completo uma vez e sai
./ekfi.py --once -n -s    # + lista de redes vizinhas e teste de velocidade
./ekfi.py -j              # dados em JSON
./ekfi.py -d 5            # atualiza a cada 5 s
./ekfi.py -h              # todas as opções
```

Para chamar como `ekfi` de qualquer pasta:

```bash
ln -s "$PWD/ekfi.py" ~/.local/bin/ekfi
```

## Abas

| # | Aba | Conteúdo |
|---|-----|----------|
| 1 | Visão geral | nota de saúde, barras de qualidade e gráficos ao vivo de sinal, latência, download e upload |
| 2 | Conexão e rádio | SSID, BSSID e fabricante do AP, segurança e cifras, PMF, padrão Wi-Fi, banda, canal, largura, DFS, potência TX, chipset e driver |
| 3 | Sinal e taxas | RSSI (atual, médio, beacons, por antena), ruído, SNR, MCS, fluxos espaciais, eficiência do link, margem de SNR, ocupação do canal |
| 4 | Tráfego | throughput atual, bytes/pacotes, retransmissões, falhas, descartes, recepção de beacons, airtime, contadores do driver |
| 5 | Rede e latência | IPs, gateway, DNS, MTU, DHCP; latência, jitter, p95 e perda até gateway/1.1.1.1/8.8.8.8; tempo de DNS |
| 6 | Vizinhança | APs co-canal e sobrepostos, interferência, APs melhores da mesma rede, melhores canais e lista de redes vizinhas |
| 7 | Estabilidade | conexões, desconexões (com motivo), roamings e beacon loss nas últimas 24 h |
| 8 | Internet | teste de velocidade (download, upload, latência HTTP, IP público) via speed.cloudflare.com |

## Atalhos

| Tecla | Ação |
|-------|------|
| `←` `→` `Tab` `1`–`8` | trocar de aba |
| `↑` `↓` `PgUp` `PgDn` `Home` `End` | rolar |
| `P` `F2` `Espaço` | pausar / retomar |
| `F4` | abrir o site distribuidor.ektech.com.br |
| `R` `F5` | novo scan de redes vizinhas |
| `S` `F6` | teste de velocidade |
| `-` `+` `F7` `F8` | intervalo de atualização |
| `O` `F9` | ordenar redes vizinhas |
| `?` `F1` | ajuda |
| `Q` `F10` | sair |

O mouse também funciona: clique no menu, nas abas e nas teclas do rodapé; a roda rola o conteúdo.

## Observações

- O dBm das redes vizinhas é estimado a partir da porcentagem informada pelo NetworkManager.
- O "throughput útil estimado" é ~65% da taxa PHY, não uma medição.
- Alguns drivers (ex.: ath10k) não informam taxa TX nem retransmissões; esses campos aparecem como `n/d` ou zero.
