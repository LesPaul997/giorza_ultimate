#!/usr/bin/env python3
"""
Script per aggiornare le coordinate degli indirizzi esistenti nel database
"""

import requests
from app import app, db
from models import DeliveryAddress

def geocode_address(indirizzo, citta, provincia, cap):
    """Geocodifica un indirizzo usando Nominatim"""
    try:
        # Costruisci l'indirizzo completo
        indirizzo_completo = f"{indirizzo}, {cap} {citta}, {provincia}, Italia"
        print(f"🔍 Geocoding: {indirizzo_completo}")
        
        # Usa Nominatim (OpenStreetMap) per il geocoding gratuito
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': indirizzo_completo,
            'format': 'json',
            'limit': 1,
            'addressdetails': 1
        }
        
        headers = {
            'User-Agent': 'EstazioneOrdini/1.0 (https://github.com/estazione-ordini; trasporti@example.com)'
        }
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data and len(data) > 0:
                coordinate_lat = float(data[0]['lat'])
                coordinate_lng = float(data[0]['lon'])
                print(f"✅ Coordinate trovate: {coordinate_lat}, {coordinate_lng}")
                return coordinate_lat, coordinate_lng
            else:
                print(f"❌ Nessun risultato trovato")
                return None, None
        else:
            print(f"❌ Errore HTTP: {response.status_code}")
            return None, None
            
    except Exception as e:
        print(f"❌ Errore durante il geocoding: {e}")
        return None, None

def update_existing_addresses():
    """Aggiorna le coordinate degli indirizzi esistenti"""
    with app.app_context():
        # Ottieni tutti gli indirizzi senza coordinate
        addresses = DeliveryAddress.query.filter(
            (DeliveryAddress.coordinate_lat.is_(None)) | 
            (DeliveryAddress.coordinate_lng.is_(None))
        ).all()
        
        print(f"📋 Trovati {len(addresses)} indirizzi da aggiornare")
        
        updated_count = 0
        for address in addresses:
            print(f"\n🔄 Aggiornando indirizzo ID {address.id}: {address.indirizzo}, {address.citta}")
            
            # Geocodifica l'indirizzo
            lat, lng = geocode_address(address.indirizzo, address.citta, address.provincia, address.cap)
            
            if lat is not None and lng is not None:
                # Aggiorna le coordinate nel database
                address.coordinate_lat = lat
                address.coordinate_lng = lng
                updated_count += 1
                print(f"✅ Aggiornato indirizzo ID {address.id}")
            else:
                print(f"❌ Impossibile geocodificare indirizzo ID {address.id}")
        
        # Commit delle modifiche
        if updated_count > 0:
            db.session.commit()
            print(f"\n🎉 Aggiornati {updated_count} indirizzi su {len(addresses)}")
        else:
            print(f"\n⚠️ Nessun indirizzo aggiornato")

if __name__ == "__main__":
    update_existing_addresses()
