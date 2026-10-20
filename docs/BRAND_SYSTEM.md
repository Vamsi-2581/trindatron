# TRINDATRON — Brand System

Uma fonte de verdade para Mobile, Web e Desktop. Este documento descreve a arte oficial, as variantes
derivadas, onde cada uma é usada e as regras que não podem ser quebradas.

> Estado: aplicado no **Mobile** (`packages/mobile`). O Web/Desktop ainda usa os arquivos antigos de
> `packages/web/public/brand/master` — ver [Pendências](#9-pendências).

---

## 1. Master artwork

| Arquivo | Dimensão | Formato | Transparência | Papel |
|---|---|---|---|---|
| `packages/mobile/assets/brand/trindatron-symbol.png` (= `packages/web/public/brand/master/trindatron-symbol.png`) | 792 × 545 | PNG RGBA | alpha **defeituoso** (bordas serrilhadas, parte da sombra de estúdio) | **Master do símbolo.** RGB intacto da renderização oficial (trefoil cromado com luz ciano). Nunca é alterado. |
| `packages/web/public/brand/master/source/logo-oficial-lockup.jpg` | 944 × 1112 | JPEG | não | **Master do lockup vertical** (símbolo + wordmark preto sobre fundo claro). Fonte do wordmark. |
| `packages/web/public/brand/master/source/logo-oficial-horizontal.jpg` | 2064 × 512 | JPEG | não | Mesma arte do lockup em faixa horizontal, com menos pixels no símbolo (~340 px). Referência. |
| `packages/web/public/brand/master/source/logos-oficiais-sheet.jpg` | 1024 × 1024 | JPEG | não | **Prancha de conceitos**, não é master: renderizações diferentes, textos gerados com erro ("CONCEPTUALCENTEMOND"), símbolo com ~360 px. Serve só como referência de intenção (versões fundo preto, fundo claro, monocromática, horizontal). |

**Por que o logo parecia "errado":** os arquivos antigos (`trindatron-icon-1024.png`, o foreground
adaptativo, `trindatron-icon-512.png` do Web e os `trindatron-symbol-*.png`) foram gerados a partir de um
recorte com alpha duro. Sobre fundo escuro isso aparece como riscos claros e escuros nas bordas, halos
cinza e restos da sombra de estúdio dentro dos laços. O símbolo em si nunca esteve errado; o recorte sim.

Depois do re-matte ainda sobrava uma faixa de borda com névoa do fundo de estúdio (halo claro) e, no
cabeçalho, o GPU reduzia o arquivo de 128 px até ~1,7× — o que serrilhava as linhas ciano finas.
Os dois pontos são tratados nas etapas 2 e 3 abaixo.

## 2. Derivação — `packages/mobile/scripts/brand/derive_brand_assets.py`

Todas as variantes do Mobile saem da master por um único script reprodutível:

1. **Re-matte:** segmentação do master com BiRefNet (`birefnet-general-lite`, rodando local) e
   descontaminação de cor das bordas (`pymatting.estimate_foreground_ml`). O resultado intacto fica em
   `trindatron-symbol-matte.png`, para as etapas seguintes não dependerem do modelo.
2. **Limpeza da borda:** alpha ≤ 36 é névoa do fundo de estúdio e sai; alpha ≥ 232 é cromo sólido; entre os
   dois a rampa é esticada. Cada pixel de borda recebe a cor do cromo sólido vizinho (média só de pixels
   sólidos), então nenhum pixel semitransparente carrega o cinza claro do estúdio. Medido no arquivo HD:
   pixels de borda 6045 → 3853, pixels de borda claros 2320 → 1395, névoa fraca 1171 → 602.
3. **Só reduções, densas:** tamanhos de UI 48 · 64 · 80 · 96 · 128 · 160 · 192 · 256 · 320 · 384 · 512
   (Lanczos, pré-multiplicado). Nenhuma tela reduz um arquivo mais de ~1,35× (coberto por `brand.test.ts`).
   O script **recusa ampliar**.
4. **Wordmark:** recortado do lockup oficial; o alpha vem da tinta preta; o arquivo é branco e é tingido
   em tempo de execução com a cor de texto do tema.
5. **Camadas da animação:** o símbolo é dividido por uma partição rígida (polígono `LOWER_LOOP_POLYGON`,
   em pixels da master; o deslocamento do matte dentro da master é localizado automaticamente) em
   "infinito superior" e "laço inferior". Em repouso, as duas camadas recompõem o símbolo pixel a pixel.
6. **Ícones:** composição do símbolo limpo sobre `#07080a` (loja/favicon) ou transparente (adaptativo).

```bash
cd packages/mobile
pip install pillow numpy "rembg[cpu]" pymatting
python scripts/brand/derive_brand_assets.py --preview <pasta>                # conferir
python scripts/brand/derive_brand_assets.py --write                          # gravar em assets/brand
# etapas 2–6 a partir do matte guardado (só Pillow):
python scripts/brand/derive_brand_assets.py --from-matte --preview <pasta>
python scripts/brand/derive_brand_assets.py --from-matte --write
```

## 3. Variantes e usos

```
MASTER (trindatron-symbol.png, logo-oficial-lockup.jpg)
  └─ re-matte ─ trindatron-symbol-matte.png (573×541, entrada reprodutível)
        └─ limpeza de borda ─ trindatron-symbol-hd.png (573×541)
              ├─ trindatron-symbol-48…512.png            logo de interface (BrandSymbol escolhe pela densidade)
              ├─ trindatron-intro-upper.png / -lower.png  animação de abertura
              ├─ trindatron-icon-1024.png                 ícone de loja / launcher iOS
              ├─ trindatron-adaptive-foreground.png       ícone adaptativo Android (+ fundo #07080a)
              ├─ trindatron-icon-32.png                   favicon Web
              └─ (splash nativo: trindatron-symbol-hd.png, 180 pt, fundo #07080a)
  └─ lockup ─ trindatron-wordmark.png (798×82)             wordmark tingível
  └─ efeito ─ trindatron-glow.png (256×256)                brilho ciano radial da abertura (efeito de luz, não é logo)
```

| Uso | Arquivo | Tamanho de exibição | Regra |
|---|---|---|---|
| Launcher Android | adaptive-foreground + `#07080a` | sistema | símbolo com 500 px dentro da zona segura de 66 % |
| Loja / iOS | icon-1024 | 1024 px | símbolo com 540 px (menor que a master: sem ampliação) |
| Splash nativo | symbol-hd | 180 pt | continua direto na animação, mesmo fundo |
| Cabeçalho da Home | `BrandLockup size="sm"` | símbolo 26 pt + wordmark 11 pt | |
| Login / Cadastro | `BrandSymbol 96` + `Wordmark` | | |
| Resposta do assistente | `BrandSymbol 16` | | marca pequena: arquivo de 48 px |
| Menu lateral | `BrandSymbol 22` | | |
| Favicon | icon-32 | 32 px | |

`BrandSymbol` (`src/components/ui/Brand.tsx`) escolhe o **menor arquivo com pixels suficientes** para a
altura pedida × densidade da tela. Nunca usa o ícone de launcher como logo de interface.

**Ícone e splash nativos vêm do build**, não do Metro: depois de mudar esses arquivos, só um novo
development/preview build mostra a versão nova no aparelho.

## 4. Cores

| Token | Escuro | Claro | Uso |
|---|---|---|---|
| `accent` | `#00c2d4` | `#00c2d4` | ciano oficial: ação principal, foco, brilho |
| `accentText` | `#5ee4f2` | `#006f7a` | texto ciano com contraste AA |
| `bg` | `#07080a` | `#eef1f4` | fundo, splash, ícone |
| `surface1…4` | `#0e1013` → `#242930` | `#f7f9fa` → `#cfd6dc` | camadas |
| `chromeHigh/Mid/Low` | `#f4f7fa` `#b9c2cc` `#9aa4b0` | `#6f7a86` `#39424c` `#1b2027` | cromo do símbolo, wordmark na abertura |
| `textPrimary/Secondary/Muted` | `#f2f5f8` `#a6aeb8` `#7e868f` | `#0b0e11` `#39424c` `#5a6570` | |

O roxo `#7c3aed` foi aposentado e não volta (coberto por `brand.test.ts`).

## 5. Espaçamento, forma e tipo

- Espaço: 4 · 8 · 12 · 16 · 24 · 32 · 48 (`space.xs…xxxl`). Margem de tela 16.
- Raio: 6 · 10 · 14 · 20 · pill (`radius`). Composer e sheets usam 20.
- Toque mínimo 44 pt. Largura máxima de conteúdo 760 pt.
- Tipo do sistema (San Francisco / Roboto): display 34/40, title 28/34, heading 18/24, body 16/24,
  caption 14/20, label 12/16 caixa-alta com tracking. O wordmark nunca é texto digitado: é a arte oficial.

## 6. Iconografia

- Ionicons: contorno (`-outline`) em repouso, preenchido quando ativo.
- 18–22 pt em linhas e botões; cor `accentText` para ações, `textSecondary` para neutras, `error` para destrutivas.
- Nenhum ícone de fornecedor, modelo ou marca de terceiros.

## 7. Abertura animada

Seis tempos, construídos com as camadas oficiais (`src/components/brand/BrandIntro.tsx`). A abertura só
começa quando as camadas estão decodificadas (espera máxima de 900 ms) e é nesse momento que o splash
nativo sai — antes, a construção acontecia atrás do splash e só o símbolo pronto aparecia.

| t (ms, após as camadas prontas) | Tempo | Evento |
|---|---|---|
| 0 | — | fundo `#07080a` continua o splash nativo |
| 0–520 | 1 | infinito superior entra de cima/esquerda (−26° → 0°) e para **afastado** do centro |
| 520–1040 | 2 | laço inferior entra sozinho de baixo/direita (+30° → 0°), também afastado |
| 1100–1560 | 3 | as duas formas se aproximam e se encaixam |
| 1560–1740 | 4 | o símbolo oficial intacto assume o quadro (as camadas coincidem pixel a pixel) |
| 1740–2560 | 5 | luz ciano sobe: halo atrás do símbolo (0 → 55 %) e brilho do centro do nó |
| 2160–2580 | 6 | wordmark aparece com leve subida (12 → 0 pt) |
| ~2880–3160 | — | pausa curta e dissolve para Login/Home |

Regras: não bloqueia o boot (a sessão carrega por baixo), ignora toques, não reinicia quando a tela de
baixo re-renderiza, respeita "reduzir movimento" (quadro final estático por 600 ms), termina na hora se
algum asset falhar e tem teto rígido de 4,8 s. Implementação nativa leve (`Animated` com driver nativo),
sem vídeo e sem dependência nova.

**Web/Desktop:** as mesmas camadas PNG e a mesma tabela de tempos em CSS/Web Animations
(`transform: translate/rotate`, `opacity`), com `prefers-reduced-motion`.

## 8. Regras do logo

1. Nunca redesenhar, vetorizar "à mão", recolorir o cromo ou trocar o ciano do símbolo.
2. Nunca ampliar um arquivo menor; gerar do master pelo script.
3. Nunca deformar (proporção 573:541), cortar, rotacionar em repouso, aplicar sombra ou contorno.
4. Área de respiro: 25 % da altura do símbolo em volta. Tamanho mínimo: 16 pt de altura.
5. Sobre fundo escuro ou claro liso. Não usar sobre fotos ou fundos com textura.
6. O wordmark é a arte do lockup, tingida com a cor de texto do tema; nunca digitado em outra fonte.
7. Compressão: PNG sem perdas para UI e ícones; JPEG só nas fontes de referência.

## 9. Pendências

- **Web/Desktop** ainda usa `trindatron-icon-512.png`, `icon-180`, `symbol-*` com o recorte defeituoso.
  Trocar pelos derivados deste script (fora do escopo do Mobile).
- **Versão monocromática** existe só na prancha de conceitos; não há master para derivar.
- A master do símbolo tem 792 × 545 (≈ 565 px de símbolo). Uma renderização oficial em resolução maior
  (≥ 2048 px) permitiria ícones e splash maiores sem limite de pixels e um matte ainda mais limpo.
