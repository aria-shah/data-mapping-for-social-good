// Curated, verified datasets organized by UN Sustainable Development Goal.

export const SDGS = [
  null,
  { n: 1, name: 'No Poverty', color: '#E5243B' },
  { n: 2, name: 'Zero Hunger', color: '#DDA63A' },
  { n: 3, name: 'Good Health', color: '#4C9F38' },
  { n: 4, name: 'Quality Education', color: '#C5192D' },
  { n: 5, name: 'Gender Equality', color: '#FF3A21' },
  { n: 6, name: 'Clean Water', color: '#26BDE2' },
  { n: 7, name: 'Clean Energy', color: '#FCC30B' },
  { n: 8, name: 'Decent Work', color: '#A21942' },
  { n: 9, name: 'Industry & Innovation', short: 'Industry', color: '#FD6925' },
  { n: 10, name: 'Reduced Inequalities', short: 'Less Inequality', color: '#DD1367' },
  { n: 11, name: 'Sustainable Cities', short: 'Cities', color: '#FD9D24' },
  { n: 12, name: 'Responsible Consumption', short: 'Consume Wisely', color: '#BF8B2E' },
  { n: 13, name: 'Climate Action', color: '#3F7E44' },
  { n: 14, name: 'Life Below Water', short: 'Ocean Life', color: '#0A97D9' },
  { n: 15, name: 'Life on Land', color: '#56C02B' },
  { n: 16, name: 'Peace & Justice', color: '#00689D' },
  { n: 17, name: 'Partnerships', short: 'Partners', color: '#19486A' },
];

// hib = higher is better (true), worse (false) or neutral (null). Drives palette choice and story wording.
export const CATALOG = [
  { source: 'wb', id: 'SI.POV.DDAY', title: 'Extreme poverty', sub: 'Share living on under $3.00/day (2021 PPP)', sdg: [1], hib: false, icon: '🪙' },
  { source: 'wb', id: 'SI.POV.GINI', title: 'Income inequality (Gini)', sub: '0 = perfect equality, 100 = maximal inequality', sdg: [10, 1], hib: false, icon: '⚖️' },
  { source: 'wb', id: 'SN.ITK.DEFC.ZS', title: 'Undernourishment', sub: 'Share of population without enough food', sdg: [2], hib: false, icon: '🍚' },
  { source: 'wb', id: 'SH.STA.STNT.ZS', title: 'Child stunting', sub: 'Children under 5 too short for their age', sdg: [2, 3], hib: false, icon: '📏' },
  { source: 'wb', id: 'SP.DYN.LE00.IN', title: 'Life expectancy', sub: 'Years a newborn is expected to live', sdg: [3], hib: true, icon: '❤️' },
  { source: 'wb', id: 'SH.DYN.MORT', title: 'Under-5 mortality', sub: 'Deaths per 1,000 live births', sdg: [3], hib: false, icon: '👶' },
  { source: 'wb', id: 'SH.STA.MMRT', title: 'Maternal mortality', sub: 'Deaths per 100,000 live births', sdg: [3, 5], hib: false, icon: '🤱' },
  { source: 'wb', id: 'SH.IMM.MEAS', title: 'Measles immunization', sub: '% of children 12–23 months', sdg: [3], hib: true, icon: '💉' },
  { source: 'wb', id: 'SH.MED.PHYS.ZS', title: 'Doctors per 1,000 people', sub: 'Physicians per 1,000 population', sdg: [3], hib: true, icon: '🩺' },
  { source: 'wb', id: 'SE.ADT.LITR.ZS', title: 'Adult literacy', sub: '% of people 15+ who can read and write', sdg: [4], hib: true, icon: '📖' },
  { source: 'wb', id: 'SE.PRM.CMPT.ZS', title: 'Primary school completion', sub: '% of relevant age group', sdg: [4], hib: true, icon: '🎒' },
  { source: 'wb', id: 'SG.GEN.PARL.ZS', title: 'Women in parliament', sub: '% of seats held by women', sdg: [5, 16], hib: true, icon: '🏛️' },
  { source: 'wb', id: 'SL.TLF.CACT.FE.ZS', title: 'Women in the workforce', sub: 'Female labor force participation (15+)', sdg: [5, 8], hib: true, icon: '👩‍🔧' },
  { source: 'wb', id: 'SP.ADO.TFRT', title: 'Adolescent births', sub: 'Births per 1,000 women aged 15–19', sdg: [5, 3], hib: false, icon: '🧒' },
  { source: 'wb', id: 'SH.H2O.BASW.ZS', title: 'Basic drinking water', sub: '% of people with at least basic access', sdg: [6], hib: true, icon: '🚰' },
  { source: 'wb', id: 'SH.STA.BASS.ZS', title: 'Basic sanitation', sub: '% of people with at least basic toilets', sdg: [6], hib: true, icon: '🚻' },
  { source: 'wb', id: 'EG.ELC.ACCS.ZS', title: 'Access to electricity', sub: '% of population', sdg: [7], hib: true, icon: '💡' },
  { source: 'wb', id: 'EG.CFT.ACCS.ZS', title: 'Clean cooking fuels', sub: '% with clean fuels & technologies', sdg: [7, 3], hib: true, icon: '🔥' },
  { source: 'wb', id: 'EG.FEC.RNEW.ZS', title: 'Renewable energy share', sub: '% of final energy consumption', sdg: [7, 13], hib: true, icon: '🌞' },
  { source: 'wb', id: 'NY.GDP.PCAP.PP.CD', title: 'GDP per person (PPP)', sub: 'Current international $', sdg: [8], hib: true, icon: '📈' },
  { source: 'wb', id: 'SL.UEM.1524.ZS', title: 'Youth unemployment', sub: '% of labor force aged 15–24', sdg: [8], hib: false, icon: '🧑‍💼' },
  { source: 'wb', id: 'IT.NET.USER.ZS', title: 'Internet users', sub: '% of population online', sdg: [9, 4], hib: true, icon: '🌐' },
  { source: 'wb', id: 'EN.ATM.PM25.MC.M3', title: 'Air pollution (PM2.5)', sub: 'Mean annual exposure, µg/m³', sdg: [11, 3], hib: false, icon: '🌫️' },
  { source: 'wb', id: 'EN.POP.SLUM.UR.ZS', title: 'Urban slum population', sub: '% of urban population', sdg: [11, 1], hib: false, icon: '🏘️' },
  { source: 'owid', id: 'co-emissions-per-capita', title: 'CO₂ emissions per person', sub: 'Tonnes per person per year', sdg: [13], hib: false, icon: '🏭' },
  { source: 'wb', id: 'AG.LND.FRST.ZS', title: 'Forest cover', sub: '% of land area', sdg: [15, 13], hib: true, icon: '🌳' },
  { source: 'wb', id: 'ER.MRN.PTMR.ZS', title: 'Marine protected areas', sub: '% of territorial waters', sdg: [14], hib: true, icon: '🐋' },
  { source: 'wb', id: 'VC.IHR.PSRC.P5', title: 'Homicide rate', sub: 'Intentional homicides per 100,000', sdg: [16], hib: false, icon: '🕊️' },
  { source: 'wb', id: 'SM.POP.REFG', title: 'Refugees hosted', sub: 'Refugee population by country of asylum', sdg: [16, 10], hib: null, icon: '⛺' },
  { source: 'owid', id: 'human-development-index', title: 'Human Development Index', sub: 'Health, education and income combined', sdg: [1, 3, 4, 8], hib: true, icon: '🌍' },
  { source: 'wb', id: 'DT.ODA.ODAT.PC.ZS', title: 'Aid received per person', sub: 'Net official development assistance (US$)', sdg: [17], hib: null, icon: '🤝' },
];

export const LIVE = [
  { source: 'url', id: 'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_month.geojson', title: 'Earthquakes this month', sub: 'USGS · magnitude 4.5+ · live feed', sdg: [11, 1], icon: '🌋', points: { value: 'mag' } },
  { source: 'url', id: 'https://eonet.gsfc.nasa.gov/api/v3/events/geojson?status=open&days=60', title: 'Active natural events', sub: 'NASA EONET · wildfires, storms, volcanoes', sdg: [13, 15], icon: '🛰️', points: { value: 'magnitudeValue', label: 'title' } },
];

export const SAMPLES = [
  { source: 'local', id: 'sample_us_states_le.csv', title: 'US life expectancy by state', sub: 'Sample CSV · ISO 3166-2 codes', sdg: [3], hib: true, icon: '🇺🇸' },
  { source: 'local', id: 'india_full_dataset.csv', title: 'India states: four indicators', sub: 'Sample CSV · life expectancy, poverty, internet, health', sdg: [1, 3, 9], hib: null, icon: '🇮🇳' },
  { source: 'local', id: 'canada_provinces_population.csv', title: 'Canada population by province', sub: 'Sample CSV', sdg: [11], hib: null, icon: '🇨🇦' },
  { source: 'local', id: 'australia_states_population.csv', title: 'Australia population by state', sub: 'Sample CSV', sdg: [11], hib: null, icon: '🇦🇺' },
];

export const QUICK_START = ['SP.DYN.LE00.IN', 'SI.POV.DDAY', 'EG.ELC.ACCS.ZS', 'SH.DYN.MORT', 'co-emissions-per-capita', 'IT.NET.USER.ZS'];

export function findCatalog(source, id) {
  return [...CATALOG, ...LIVE, ...SAMPLES].find((c) => c.source === source && c.id === id) || null;
}
