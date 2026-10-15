# PDF Düzenleyici

PDF içinde alan silme, yazı ekleme ve mevcut yazıyı değiştirme yapabilen,
Adobe benzeri koyu arayüzlü masaüstü uygulaması. PySide6 (Qt) + PyMuPDF.

## Başlatma

Masaüstündeki **PDF Düzenleyici** kısayoluna çift tıklayın, ya da:

```powershell
C:\Users\Burak\Desktop\PDF Editor\.venv\Scripts\pythonw.exe "C:\Users\Burak\Desktop\PDF Editor\main.py"
```

Kısayola bir PDF sürükleyip bırakarak da doğrudan o dosyayla açabilirsiniz.

## Özellikler

- **Sürekli kaydırma**: sayfalar alt alta dizilir, normal PDF okuyucu gibi mouse
  tekerleğiyle gezersiniz; üstteki gösterge bulunduğunuz sayfayı takip eder.
  Sayfalar tembel render edilir (büyük PDF'lerde bellek dostu).
- **Doğrudan paragraf düzenleme (varsayılan)**: herhangi bir kelimeye tıklayın.
  Uygulama kelimenin ait olduğu metin bloğunun bütün satırlarını tek, çerçevesiz
  yazı akışı olarak açar. Kelime silme/yazma normal bir belge gibi sonraki
  satırlara akar; yukarı/aşağı yön tuşlarıyla satırlar arasında gezilir, Enter
  yeni satır açar, Ctrl+Enter kaydeder ve Esc vazgeçer. Yalnız işletim sisteminin
  doğal metin imleci kullanılır; ikinci bir yapay imleç çizilmez. Metni açmak,
  seçmek veya yön tuşlarıyla gezmek alttaki kaynak PDF piksellerini/fontunu
  yeniden çizmez.
- **Taranmış PDF OCR**: metin katmanı olmayan taramalarda yerel Tesseract ve
  Türkçe model otomatik devreye girer. Bir kelime değiştirildiğinde satırın
  değişmeyen bölümleri yeniden yazılmaz; orijinal tarama pikselleri birebir
  korunur. Yeni kelimenin harfleri de mümkün olduğunda aynı sayfadaki gerçek
  taranmış karakter piksellerinden klonlanır; renk, kenar yumuşaklığı, baskı
  kalınlığı ve tarama dokusu yeniden üretilmez. Yalnız sayfada hiç örneği olmayan
  bir karakter için kalibre edilmiş gerçek Windows fontuna dönülür. Belge
  bilgisayardan dışarı gönderilmez.
- **Kararlı düzenleme sahnesi**: OCR yalnız belgenin değişmez ilk taraması
  üzerinde bir kez çalışır. Düzenlenmiş çıktı tekrar OCR edilmez; satır, kelime,
  hizalama ve font bilgisi aynı sahne modelinde güncellenir. Her değişiklikte
  yeni PNG katmanı eklemek yerine mevcut tam-sayfa image xref'i kayıpsız olarak
  yenilenir. Böylece ardışık düzenlemelerde eski yazı geri sızmaz, katman
  birikmez ve Ctrl+Z metin haritasını da doğru revizyona geri alır.
- **Paragraf yeniden akışı**: uzayan bir kelime sağ kenardan dışarı çıkmaz;
  paragrafın devamındaki satırlara aktarılır. Yeri değişen mevcut kelimeler fontla
  yeniden üretilmez, özgün taramadan tek parça piksel olarak taşınır. Böylece
  harf şekli, `y` gibi alt uzantılar, tarama yumuşaklığı ve renk korunur.
- **Kelime içi fark**: `merkezlerinde → merkezlerindas` gibi bir değişiklikte
  kelimenin tamamı yeniden kurulmaz. Ortak `merkezlerind` bölümü kaynak görüntüde
  kalır; yalnız `as` üretilir. Karakter sınırları ham LSTM kutularına güvenmek
  yerine kelime görüntüsündeki gerçek mürekkep boşluklarıyla bulunur.
- **Tablo hücresi**: taranmış veya vektörel tablolarda hücre sınırları analiz
  edilir. Boş değer hücresine tıklayarak da hücrenin içine, sığdırılmış biçimde
  yazabilirsiniz.
- **Sil**: alanı sürükleyerek seçin. Taranmış sayfalarda eski alan düz beyazla
  kapatılmaz; çevredeki kâğıt rengi, geçişi ve tarama dokusu kullanılarak onarılır.
- **Yazı Ekle**: eklemek istediğiniz noktaya tıklayın. Tıkladığınız satırdaki
  yazının **fontu, boyutu, rengi ve kalın/italik durumu otomatik algılanır**;
  yeni yazı tamamen aynı stilde eklenir. Dialog algılanan stili gösterir,
  isterseniz boyutu değiştirebilirsiniz. Uzun metin sayfa/hücre sağ sınırında
  otomatik sarılır; dikey alan da yetmezse taşırmak yerine yalnız gerektiği kadar
  küçültülür.
- **Nesne**: tablo, görsel veya şekli çerçeveleyin. Seçimin içinden sürükleyerek
  taşıyın; sekiz tutamacından boyutlandırın. Eski konum tarama dokusuyla onarılır.
- **Araç geçişi**: Sil, Yazı ekle ve Nesne arasında doğrudan geçebilirsiniz.
  Seçili araca yeniden tıklamak aracı kapatıp varsayılan metin düzenlemeye döner.
- **Geri Al** (Ctrl+Z): son 20 işlem.
- **Kaydet / Farklı Kaydet** (Ctrl+S).

## Stil eşleştirme nasıl çalışır?

### Taranmış PDF

1. Tesseract satır, kelime ve gerçek Unicode karakter kutularını tek geçişte
   çıkarır.
2. Düzenlenen satır için aynı punto, aile ve kalınlıktaki karakter örnekleri
   sayfanın özgün tarama görselinden alınır.
3. Yeni kelime, bu karakterlerin gözlenen RGB pikselleriyle kayıpsız biçimde
   kurulur. Önceki/sonraki kelimeler yeniden render edilmez; sayfanın güncel
   tek görüntü nesnesi yenilenir.
4. Gerekli karakter sayfanın hiçbir yerinde yoksa yalnız o durumda TTF yedeği
   kullanılır.

### Vektörel PDF

1. Önce PDF'e gömülü **orijinal font** çıkarılıp yeniden kullanılmaya çalışılır
   (yazacağınız harfler subset fontta eksikse vazgeçilir).
2. Olmazsa font adına göre Windows'taki en yakın TTF seçilir
   (Times, Arial, Calibri, Courier, Verdana, Georgia... + kalın/italik varyantları).
3. O da olmazsa Türkçe destekli Arial'e düşülür.

## Notlar

- Vektörel PDF'te değiştirme, gerçek font kaynağıyla yeniden yazılır. Taranmış
  PDF'te yalnız değişen bölüm üretilir; eşit kalan kelimeler piksel olarak korunur
  ve sonraki kelimeler yeni genişliğe göre doğal biçimde kaydırılır.
- Taranmış sayfada bulunmayan yepyeni bir karakterin kaynak pikseli fiziksel
  olarak mevcut değildir; bu tek durumda matematiksel piksel eşitliği mümkün
  olmaz ve kalibre edilmiş font yedeği devreye girer. Kaynak karakter mevcutsa
  uygulama font tahmini yapmak yerine doğrudan o tarama piksellerini kullanır.
- Taranmış sayfada ilk metin haritasının hazırlanması birkaç saniye sürebilir;
  işlem arka planda yapılır ve sonraki tıklamalar önbellekten cevaplanır.
- Yeni satır bulunduğu satır veya hücreye sığmazsa font yalnız gerektiği kadar
  küçültülür.
- Vektörel bir nesne taşındığında seçili görünüm yüksek çözünürlüklü bir görsel
  katmanı olarak taşınır. Bu, görünümü korur; taşınan bölgenin içindeki ayrı
  vektör öğeler tek tek ayrıştırılmaz.
