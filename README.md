# Connection Monitor

Connection Monitor is een kleine Windows-applicatie voor het langdurig controleren van netwerkverbindingen. Het programma pingt maximaal tien zelfgekozen IP-adressen of hostnamen en registreert wanneer een doel onbereikbaar wordt en wanneer de verbinding herstelt.

De applicatie is bedoeld om terugkerende netwerkproblemen zichtbaar te maken. Door bijvoorbeeld de lokale router, het modem, een DNS-server en een extern adres tegelijk te volgen, is beter te bepalen in welk deel van de verbinding een storing ontstaat.

## Mogelijkheden

- maximaal tien IP-adressen of hostnamen tegelijk bewaken;
- meetinterval, timeout en storingsgrens instellen;
- status, latency en pakketverlies per sessie tonen;
- begin, einde en duur van storingen vastleggen;
- tijdlijn met een schaal van 10 minuten tot 24 uur;
- tijden met milliseconden registreren;
- instellingen lokaal bewaren;
- eerdere storingen na een herstart teruglezen;
- een lopende storing na een herstart voortzetten;
- optioneel RouterOS-logs via SSH uitlezen;
- optioneel extra RouterOS-logging voor interface-, route- en DHCP-gebeurtenissen inschakelen.

De pingopdrachten voor de verschillende doelen worden parallel uitgevoerd. De grafische interface blijft tijdens het meten beschikbaar.

## Systeemvereisten

- Windows 10 of Windows 11;
- Python 3.10 of nieuwer;
- toegang tot het Windows-programma `ping`;
- voor de optionele RouterOS-functies: SSH-toegang tot de MikroTik-router.

De applicatie gebruikt alleen de standaardbibliotheek van Python, aangevuld met `paramiko` en `keyring` voor RouterOS en veilige wachtwoordopslag.

## Installatie

Download of clone de repository en open PowerShell in de projectmap.

Maak desgewenst eerst een virtuele omgeving:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Installeer de dependencies:

```powershell
py -m pip install -r requirements.txt
```

Start vervolgens het programma:

```powershell
py mikrotik_connection_monitor.py
```

Het bestand `monitor_icon.ico` moet in dezelfde map staan als het Python-bestand.

## Eerste configuratie

Bij de eerste start opent automatisch het instellingenscherm. Per meetdoel zijn een naam en een IP-adres of hostnaam nodig. Lege regels worden genegeerd.

Een bruikbare basisconfiguratie is:

| Naam | Adres | Functie |
| --- | --- | --- |
| Router | `192.168.178.1` | Controle van het lokale netwerk en de router |
| Modem | `192.168.88.1` | Controle van de verbinding tussen router en modem |
| Internet | `1.1.1.1` | Controle van externe bereikbaarheid |
| Tweede externe controle | `8.8.8.8` | Vergelijking met een tweede externe bestemming |

Andere nuttige doelen zijn een lokale DNS-server, een bekabeld apparaat dat altijd aanstaat en een hostname voor een afzonderlijke DNS-controle.

De configuratie wordt opgeslagen in `connection_monitor_config.json`. Dit bestand wordt niet in Git opgenomen.

## Storingsregistratie

Een storing begint standaard na twee opeenvolgende mislukte pings. Deze grens is instelbaar. Een enkele verloren ping wordt daardoor niet direct als storing geregistreerd.

De kolom `Verlies sessie` begint bij iedere start van het programma opnieuw op nul. Het totale aantal storingen en de laatste storing worden uit het gebeurtenissenbestand hersteld.

De tijdlijn gebruikt:

- groen voor een bereikbaar doel;
- rood voor een geregistreerde storing.

De schaal kan tijdens het gebruik worden gewijzigd. De gekozen schaal wordt in de configuratie bewaard.

## Logbestanden

De gekozen logmap bevat:

| Bestand | Inhoud |
| --- | --- |
| `connection_samples.csv` | Iedere meting met tijdstip, status, latency en eventuele foutmelding |
| `connection_events.csv` | Begin, herstel en duur van iedere storing |
| `routeros_snapshot_*.txt` | Handmatig of automatisch opgehaalde RouterOS-informatie |

De CSV-bestanden kunnen rechtstreeks in Excel, LibreOffice Calc of een analysetool worden geopend.

## RouterOS

RouterOS-ondersteuning is optioneel. De verbinding loopt via SSH. Het RouterOS-adres, de poort en de gebruikersnaam staan in het configuratiebestand. Het wachtwoord kan via Windows Credential Manager worden bewaard en wordt niet als leesbare tekst in het JSON-bestand opgeslagen.

Na de eerste geslaagde verbinding wordt de SSH-hostkey opgeslagen in `routeros_known_hosts`. Een onverwachte wijziging van deze sleutel wordt daarna geweigerd. Controleer bij een wijziging eerst of de router opnieuw is geïnstalleerd of vervangen voordat dit bestand wordt verwijderd.

De knop `Extra logging aan` voegt alleen regels met de prefix `PYCONMON` toe. De knop `Extra logging uit` verwijdert uitsluitend regels met diezelfde prefix. Bestaande RouterOS-logregels worden niet aangepast.

Gebruik bij voorkeur een afzonderlijke RouterOS-gebruiker met alleen de benodigde rechten. Stel de SSH-service niet open naar internet.

## Nauwkeurigheid

De tijdstempels worden met milliseconden opgeslagen. Zij geven het moment aan waarop het afzonderlijke pingproces een resultaat teruggeeft. Dat is niet hetzelfde als een meting van het werkelijke fysieke uitvalmoment met millisecondeprecisie. Kleine verschillen kunnen worden veroorzaakt door Windows, procesplanning en netwerkvertraging.

Sommige apparaten en internetdiensten beantwoorden ping beperkt of helemaal niet. Een mislukte ping betekent daarom niet in alle situaties dat normaal netwerkverkeer onmogelijk is.
