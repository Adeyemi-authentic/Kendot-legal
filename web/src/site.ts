// Firm-level settings in one place. For a client build, edit this file,
// global.css (brand tokens) and src/content/, and the site is rebranded.
export const SITE = {
  name: 'Kendot Legal',
  tagline: 'Clear legal advice for businesses and property in Nigeria',
  description:
    'Kendot Legal is a commercial law firm in Lagos and Abuja advising on corporate, real estate and tenancy, tax, intellectual property, data protection and disputes.',
  concept: true, // shows the "fictional concept build" notice
  email: 'hello@kendotlegal.example',
  phone: '+234 700 000 0001',
  whatsapp: '+234 700 000 0003',
  offices: [
    { city: 'Lagos', address: '12 Kendot Close, Lekki Phase 1, Lagos' },
    { city: 'Abuja', address: 'Suite 4, Kendot House, Wuse 2, Abuja' },
  ],
  hours: 'Monday to Friday, 8:30am to 5:30pm (WAT)',
  nav: [
    { href: '/practice/', label: 'Practice areas' },
    { href: '/people/', label: 'People' },
    { href: '/insights/', label: 'Insights' },
    { href: '/about/', label: 'About' },
    { href: '/faqs/', label: 'FAQs' },
  ],
};
