# ekfi
Smart monitor for WLAN/Wi-Fi Site

**Ekfi by Ektech** é um monitor interativo de terminal (no estilo do `htop`) que mostra os KPIs do Wi-Fi ao qual você está conectado, atualizados em tempo real. Funciona no **Linux**, no **Windows** e no **macOS**.

## Instalação e uso

Precisa de Python 3.8 ou mais recente. Não usa bibliotecas externas, exceto as opcionais indicadas abaixo, e não precisa de root nem de administrador.

### Linux

```bash
./ekfi.py
```

Usa `iw`, `nmcli` (NetworkManager) e `ping`, que já vêm na maioria das distribuições. `ethtool`, `lspci` e `journalctl` são opcionais e acrescentam contadores do driver, o nome do chipset e o histórico de conexões.

Para chamar como `ekfi` de qualquer pasta:

```bash
ln -s "$PWD/ekfi.py" ~/.local/bin/ekfi
```

### Windows 10/11

```powershell
pip install windows-curses   # necessário para o modo interativo
python ekfi.py
```

Use o **Windows Terminal** para ver as cores e os caracteres corretamente. Sem o `windows-curses`, o Ekfi mostra o relatório completo (`--once`) em vez da tela interativa.

No Windows 11 24H2 ou mais recente, o sistema só informa o SSID, o BSSID e as redes vizinhas se a localização estiver liberada: **Configurações > Privacidade e segurança > Localização > Permitir que aplicativos da área de trabalho acessem sua localização**.

### macOS

```bash
python3 ekfi.py
pip3 install pyobjc-framework-CoreWLAN   # opcional, recomendado
```

Sem o CoreWLAN, o Ekfi usa o `system_profiler`, que é mais lento (a atualização fica mais espaçada) e não informa BSSID nem potência TX.

A partir do macOS 14, o SSID e o BSSID só aparecem se o Terminal tiver permissão de localização: **Ajustes do Sistema > Privacidade e Segurança > Serviços de Localização**.

### Opções

```bash
ekfi.py                 # monitor interativo
ekfi.py --once          # relatório completo uma vez e sai
ekfi.py --once -n -s    # + lista de redes vizinhas e teste de velocidade
ekfi.py -j              # dados em JSON
ekfi.py -d 5            # atualiza a cada 5 s
ekfi.py -i wlan0        # escolhe a interface (no Windows aceita nome ou descrição do adaptador)
ekfi.py -h              # todas as opções
```

## Abas

| # | Aba | Conteúdo |
|---|-----|----------|
| 1 | Visão geral | logo, nota de saúde, barras de qualidade e gráficos ao vivo de sinal, latência, download e upload |
| 2 | Conexão e rádio | SSID, BSSID e fabricante do AP, segurança e cifras, PMF, padrão Wi-Fi, banda, canal, largura, DFS, potência TX, chipset e driver |
| 3 | Sinal e taxas | RSSI (atual, médio, beacons, por antena), ruído, SNR, MCS, fluxos espaciais, eficiência do link, margem de SNR, ocupação do canal |
| 4 | Tráfego | throughput atual, bytes/pacotes, retransmissões, falhas, descartes, recepção de beacons, airtime, contadores do driver |
| 5 | Rede e latência | IPs, gateway, DNS, MTU, DHCP; latência, jitter, p95 e perda até gateway/1.1.1.1/8.8.8.8; tempo de resposta de cada DNS |
| 6 | Vizinhança | APs co-canal e sobrepostos, interferência, APs melhores da mesma rede, melhores canais e lista de redes vizinhas |
| 7 | Estabilidade | conexões, desconexões (com motivo), roamings e falhas nas últimas 24 h |
| 8 | Internet | teste de velocidade (download, upload, latência HTTP, IP público) via speed.cloudflare.com |

## O que cada sistema informa

Cada sistema expõe dados diferentes. O que não está disponível aparece como `n/d`.

| KPI | Linux | Windows | macOS |
|-----|:-----:|:-------:|:-----:|
| SSID, BSSID, canal, banda, largura | ✓ | ✓ | ✓ (BSSID só com CoreWLAN) |
| RSSI | ✓ | ✓ | ✓ |
| Ruído e SNR | ✓ | — | ✓ |
| RSSI médio, por antena e dos beacons | ✓ | — | — |
| Taxa RX / TX | ✓ | ✓ | só TX |
| MCS, fluxos espaciais, intervalo de guarda | ✓ | — | MCS (sem CoreWLAN) |
| Segurança e cifras | ✓ | ✓ | ✓ |
| Potência TX | ✓ | — | ✓ (CoreWLAN) |
| Ocupação do canal | ✓ | — | — |
| Retransmissões e falhas de TX | ✓ | ✓ | — |
| Beacons, airtime, contadores do driver | ✓ | parcial (FCS, ACK) | — |
| Throughput atual | ✓ | ✓ | ✓ |
| IP, gateway, DNS, DHCP | ✓ | ✓ | ✓ |
| Latência, jitter, perda, DNS, speedtest | ✓ | ✓ | ✓ |
| Redes vizinhas e melhores canais | ✓ | ✓ | ✓ |
| Histórico de conexões (24 h) | ✓ | ✓ | — |

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

- No Linux, o dBm das redes vizinhas é estimado a partir da porcentagem informada pelo NetworkManager. No Windows e no macOS, o valor vem direto do sistema.
- O "throughput útil estimado" é ~65% da taxa PHY, não uma medição.
- Quando o sistema não informa quantas antenas a placa tem (Windows e macOS), o PHY máximo é estimado pela taxa atual e a eficiência do link não é calculada.
- Alguns drivers (ex.: ath10k) não informam taxa TX nem retransmissões; esses campos aparecem como `n/d` ou zero.
