plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing comes from GitHub Actions secrets (see .github/workflows/android.yml). One fixed key, so a
// new APK installs over the old one; without the secrets (a local build) the debug key is used.
val keystorePath: String? = System.getenv("ARYA_KEYSTORE")

android {
    namespace = "com.arya.phone"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.arya.phone"
        minSdk = 26
        targetSdk = 34
        versionCode = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
        versionName = "1.0.${System.getenv("GITHUB_RUN_NUMBER") ?: "0"}"
    }

    signingConfigs {
        if (keystorePath != null) {
            create("release") {
                storeFile = file(keystorePath)
                storePassword = System.getenv("ARYA_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ARYA_KEY_ALIAS")
                keyPassword = System.getenv("ARYA_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (keystorePath != null) signingConfigs.getByName("release")
                            else signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // Google's code scanner: scans the pairing QR code without the app needing camera permission.
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
}
